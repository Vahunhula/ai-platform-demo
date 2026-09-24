"""Phase 2 workflow phase, artifact, checklist, and migration tests."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_platform.api.app import create_app
from ai_platform.auth import AuthenticatedUser, Role
from ai_platform.events import EventType
from ai_platform.models import TaskDefinition, TaskDifficulty, TaskStatus
from ai_platform.storage import SQLiteStorage
from ai_platform.workflow import (
    ArtifactKind,
    ChecklistItem,
    ChecklistResult,
    ChecklistStatus,
    TransitionMode,
    WorkflowPhase,
    calculate_readiness,
)
from ai_platform.workflow_services import (
    ChecklistService,
    WorkflowArtifactService,
    WorkflowConflictError,
    WorkflowError,
    WorkflowPhaseService,
)
from tests.test_messaging import _client, _context


def _task(task_id: str = "TEST-1") -> TaskDefinition:
    return TaskDefinition(
        id=task_id,
        title="Workflow task",
        description="Exercise the workflow domain",
        difficulty=TaskDifficulty.MEDIUM,
        acceptance_criteria=["Remain auditable"],
        verification={"type": "pytest", "targets": ["tests"]},
    )


def _storage(tmp_path: Path, *task_ids: str) -> SQLiteStorage:
    storage = SQLiteStorage(tmp_path / "platform.db")
    storage.initialize()
    for task_id in task_ids or ("TEST-1",):
        storage.create_task(_task(task_id))
    return storage


def _developer(username: str = "dev") -> AuthenticatedUser:
    return AuthenticatedUser(f"{username}-id", username, username.title(), Role.DEVELOPER)


def _payload(kind: ArtifactKind) -> dict[str, object]:
    return {
        ArtifactKind.BRAINSTORM_SUMMARY: {
            "summary": "Explore it",
            "assumptions": [],
            "options": ["A"],
            "questions": [],
        },
        ArtifactKind.PLAN: {
            "summary": "Implement it",
            "files": ["app.py"],
            "steps": ["Change code"],
            "tests": ["pytest"],
            "risks": [],
            "open_questions": [],
        },
        ArtifactKind.IMPLEMENTATION_SUMMARY: {
            "summary": "Implemented",
            "files_changed": ["app.py"],
            "tests_run": ["pytest: passed"],
            "known_issues": [],
        },
        ArtifactKind.REVIEW_REPORT: {
            "summary": "Reviewed",
            "critical_findings": [],
            "major_findings": [],
            "minor_findings": [],
            "requirements_assessment": "Covered",
            "test_assessment": "Passing",
            "convention_assessment": "Consistent",
        },
        ArtifactKind.HUMAN_REVIEW_DECISION: {
            "decision": "APPROVED",
            "feedback": "Looks good",
            "target_phase": None,
        },
    }[kind]


def test_static_tasks_default_to_implementation_and_status_is_independent(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    record = storage.get_task("TEST-1")
    assert record is not None
    assert record.status is TaskStatus.READY
    assert record.workflow_phase is WorkflowPhase.IMPLEMENTATION

    storage.update_task_status("TEST-1", TaskStatus.COMPLETED)
    updated = storage.get_task("TEST-1")
    assert updated is not None
    assert updated.status is TaskStatus.COMPLETED
    assert updated.workflow_phase is WorkflowPhase.IMPLEMENTATION
    with (
        pytest.raises(sqlite3.IntegrityError, match="invalid workflow phase"),
        storage.transaction() as connection,
    ):
        connection.execute("UPDATE tasks SET workflow_phase = 'COMPLETED' WHERE task_id = 'TEST-1'")


def test_phase_migration_policy_is_deterministic_and_repeat_safe(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT,
            difficulty TEXT NOT NULL, status TEXT NOT NULL, selected_tier TEXT,
            selected_model TEXT, attempt INTEGER NOT NULL DEFAULT 0,
            verification_status TEXT NOT NULL DEFAULT 'not_run', workspace_path TEXT,
            active_execution TEXT, execution_owner TEXT, execution_id TEXT,
            execution_actor_id TEXT, execution_pid INTEGER, execution_hostname TEXT,
            execution_started_at TEXT, execution_heartbeat_at TEXT,
            pause_requested INTEGER NOT NULL DEFAULT 0, repository_id TEXT,
            base_branch TEXT, assignee_user_id TEXT, jira_key TEXT, created_by TEXT,
            acceptance_criteria_json TEXT, verification_json TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE events (
            sequence_id INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
            task_id TEXT NOT NULL, timestamp TEXT NOT NULL, event_type TEXT NOT NULL,
            actor_type TEXT NOT NULL, actor_id TEXT NOT NULL, metadata_json TEXT NOT NULL
        );
        """
    )
    base = ("medium", "ready", "2026-01-01T00:00:00+00:00")
    connection.execute(
        "INSERT INTO tasks (task_id,title,difficulty,status,created_at,updated_at) "
        "VALUES ('DEMO-1','Legacy',?,?,?,?)",
        (*base, base[2]),
    )
    connection.execute(
        "INSERT INTO tasks (task_id,title,difficulty,status,repository_id,created_at,updated_at) "
        "VALUES ('NEW-1','New',?,?, 'repo',?,?)",
        (*base, base[2]),
    )
    connection.execute(
        "INSERT INTO tasks (task_id,title,difficulty,status,repository_id,created_at,updated_at) "
        "VALUES ('RUN-1','Run',?,?, 'repo',?,?)",
        (*base, base[2]),
    )
    connection.execute(
        "INSERT INTO events (id,task_id,timestamp,event_type,actor_type,actor_id,metadata_json) "
        "VALUES ('event-1','RUN-1',?,'TASK_STARTED','human','dev','{}')",
        (base[2],),
    )
    connection.commit()
    connection.close()

    storage = SQLiteStorage(db_path)
    storage.initialize()
    storage.initialize()
    assert storage.get_task("DEMO-1").workflow_phase is WorkflowPhase.IMPLEMENTATION
    assert storage.get_task("NEW-1").workflow_phase is WorkflowPhase.BRAINSTORM
    assert storage.get_task("RUN-1").workflow_phase is WorkflowPhase.IMPLEMENTATION


def test_manual_forward_backtrack_and_durable_actor_event(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    service = WorkflowPhaseService(storage)
    actor = _developer("alice")
    service.transition("TEST-1", WorkflowPhase.IMPLEMENTATION, WorkflowPhase.REVIEW, actor)
    service.transition(
        "TEST-1",
        WorkflowPhase.REVIEW,
        WorkflowPhase.PLAN,
        actor,
        reason="Revise the test plan",
    )
    record = storage.get_task("TEST-1")
    assert record is not None and record.workflow_phase is WorkflowPhase.PLAN
    events = [
        event
        for event in storage.get_events("TEST-1")
        if event.event_type is EventType.WORKFLOW_PHASE_CHANGED
    ]
    assert [event.actor_id for event in events] == ["alice", "alice"]
    assert events[-1].metadata == {
        "display_name": "Alice",
        "from_phase": "REVIEW",
        "reason": "Revise the test plan",
        "to_phase": "PLAN",
        "transition_mode": "MANUAL",
    }
    with pytest.raises(WorkflowError, match="not enabled"):
        service.transition(
            "TEST-1",
            WorkflowPhase.PLAN,
            WorkflowPhase.IMPLEMENTATION,
            actor,
            mode=TransitionMode.AUTOMATIC,
        )


def test_conflicting_transition_serializes_and_separate_tasks_succeed(tmp_path: Path) -> None:
    storage = _storage(tmp_path, "A", "B")
    actor = _developer()
    service = WorkflowPhaseService(storage)
    barrier = threading.Barrier(2)

    def race(target: WorkflowPhase) -> str:
        barrier.wait()
        try:
            service.transition("A", WorkflowPhase.IMPLEMENTATION, target, actor)
            return "changed"
        except WorkflowConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race, [WorkflowPhase.PLAN, WorkflowPhase.REVIEW]))
    assert sorted(results) == ["changed", "conflict"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        phases = list(
            pool.map(
                lambda task_id: service.transition(
                    task_id,
                    storage.get_task(task_id).workflow_phase,
                    WorkflowPhase.HUMAN_REVIEW,
                    actor,
                ),
                ["A", "B"],
            )
        )
    assert phases == [WorkflowPhase.HUMAN_REVIEW, WorkflowPhase.HUMAN_REVIEW]


def test_all_artifact_contracts_versions_history_and_current(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    service = WorkflowArtifactService(storage)
    actor = _developer()
    artifacts = []
    phases = {
        ArtifactKind.BRAINSTORM_SUMMARY: WorkflowPhase.BRAINSTORM,
        ArtifactKind.PLAN: WorkflowPhase.PLAN,
        ArtifactKind.IMPLEMENTATION_SUMMARY: WorkflowPhase.IMPLEMENTATION,
        ArtifactKind.REVIEW_REPORT: WorkflowPhase.REVIEW,
        ArtifactKind.HUMAN_REVIEW_DECISION: WorkflowPhase.HUMAN_REVIEW,
    }
    for kind, phase in phases.items():
        artifacts.append(service.create("TEST-1", phase, kind, _payload(kind), actor))
    plan_v2 = service.create(
        "TEST-1", WorkflowPhase.PLAN, ArtifactKind.PLAN, _payload(ArtifactKind.PLAN), actor
    )
    history = storage.list_workflow_artifacts("TEST-1")
    current = storage.list_workflow_artifacts("TEST-1", current_only=True)
    plan_v1 = next(item for item in artifacts if item.kind is ArtifactKind.PLAN)
    assert len(history) == 6 and len(current) == 5
    assert plan_v2.version == 2 and plan_v2.supersedes_artifact_id == plan_v1.artifact_id
    assert {item.version for item in history if item.kind is ArtifactKind.PLAN} == {1, 2}


def test_artifact_validation_task_isolation_and_concurrent_versions(tmp_path: Path) -> None:
    storage = _storage(tmp_path, "A", "B")
    service = WorkflowArtifactService(storage)
    actor = _developer()
    with pytest.raises(WorkflowError, match="Invalid PLAN"):
        service.create("A", WorkflowPhase.PLAN, ArtifactKind.PLAN, {"summary": "x"}, actor)
    with pytest.raises(WorkflowError, match="belongs to PLAN"):
        service.create(
            "A",
            WorkflowPhase.REVIEW,
            ArtifactKind.PLAN,
            _payload(ArtifactKind.PLAN),
            actor,
        )

    barrier = threading.Barrier(8)

    def create(_index: int) -> int:
        barrier.wait()
        return service.create(
            "A", WorkflowPhase.PLAN, ArtifactKind.PLAN, _payload(ArtifactKind.PLAN), actor
        ).version

    with ThreadPoolExecutor(max_workers=8) as pool:
        versions = sorted(pool.map(create, range(8)))
    assert versions == list(range(1, 9))
    assert storage.list_workflow_artifacts("B") == []


def _item(
    key: str,
    weight: int,
    status: ChecklistStatus,
    *,
    blocking: bool = False,
) -> ChecklistItem:
    return ChecklistItem(
        key=key,
        label=key,
        weight=weight,
        blocking=blocking,
        status=status,
        evidence=f"evidence for {key}",
    )


def test_readiness_formula_threshold_blockers_and_zero_are_deterministic() -> None:
    below = [
        _item("pass_item", 9799, ChecklistStatus.PASS),
        _item("fail_item", 201, ChecklistStatus.FAIL),
    ]
    assert calculate_readiness(below).score == 97.99
    assert not calculate_readiness(below).eligible_for_auto_progression

    threshold = [
        _item("pass_item", 98, ChecklistStatus.PASS),
        _item("fail_item", 2, ChecklistStatus.FAIL),
    ]
    assert calculate_readiness(threshold).eligible_for_auto_progression
    blocking_fail = [_item("block", 0, ChecklistStatus.FAIL, blocking=True), *threshold]
    blocking_human = [_item("human", 0, ChecklistStatus.NEEDS_HUMAN, blocking=True), *threshold]
    assert not calculate_readiness(blocking_fail).eligible_for_auto_progression
    assert not calculate_readiness(blocking_human).eligible_for_auto_progression
    assert calculate_readiness([]).model_dump() == {
        "score": 0.0,
        "blocking_failures": [],
        "blocking_needs_human": [],
        "eligible_for_auto_progression": False,
    }
    assert calculate_readiness(threshold) == calculate_readiness(threshold)


def test_checklist_status_validation_history_evidence_and_concurrency(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        ChecklistResult(key="scope_complete", status="MAYBE", evidence="no")
    storage = _storage(tmp_path)
    service = ChecklistService(storage)
    actor = _developer()
    results = [
        ChecklistResult(key="scope_complete", status="PASS", evidence="Scope listed"),
        ChecklistResult(key="tests_defined", status="FAIL", evidence="No test yet"),
        ChecklistResult(key="risks_addressed", status="PASS", evidence="Risks listed"),
    ]
    first = service.evaluate("TEST-1", WorkflowPhase.PLAN, results, actor)
    assert first.readiness.score == 70
    assert first.readiness.blocking_failures == ["tests_defined"]
    assert not first.readiness.eligible_for_auto_progression
    incomplete = service.evaluate(
        "TEST-1",
        WorkflowPhase.PLAN,
        [ChecklistResult(key="scope_complete", status="PASS", evidence="Scope listed")],
        actor,
    )
    assert len(incomplete.items) == 3
    assert incomplete.items[1].status is ChecklistStatus.NEEDS_HUMAN
    assert not incomplete.readiness.eligible_for_auto_progression
    barrier = threading.Barrier(4)

    def evaluate(_index: int) -> int:
        barrier.wait()
        return service.evaluate("TEST-1", WorkflowPhase.PLAN, results, actor).evaluation_number

    with ThreadPoolExecutor(max_workers=4) as pool:
        numbers = sorted(pool.map(evaluate, range(4)))
    assert numbers == [3, 4, 5, 6]
    history = storage.list_checklist_evaluations("TEST-1")
    assert len(history) == 6 and history[0].items[1].evidence == "No test yet"


def test_reset_preserves_phase_artifacts_and_checklists(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    actor = _developer()
    WorkflowPhaseService(storage).transition(
        "TEST-1", WorkflowPhase.IMPLEMENTATION, WorkflowPhase.PLAN, actor
    )
    WorkflowArtifactService(storage).create(
        "TEST-1", WorkflowPhase.PLAN, ArtifactKind.PLAN, _payload(ArtifactKind.PLAN), actor
    )
    ChecklistService(storage).evaluate("TEST-1", WorkflowPhase.PLAN, [], actor)
    storage.update_task_status("TEST-1", TaskStatus.FAILED)
    storage.reset_task_runtime("TEST-1")
    record = storage.get_task("TEST-1")
    assert record is not None
    assert record.status is TaskStatus.READY and record.workflow_phase is WorkflowPhase.PLAN
    assert len(storage.list_workflow_artifacts("TEST-1")) == 1
    assert len(storage.list_checklist_evaluations("TEST-1")) == 1


@pytest.mark.anyio
async def test_phase_api_authorization_actor_spoof_and_manual_gate_override(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    app = create_app(context)
    task_id = "DEMO-1"
    body = {"from_phase": "IMPLEMENTATION", "to_phase": "PLAN", "reason": "Manual"}
    async with _client(app, "watcher", Role.VIEWER) as viewer:
        assert (await viewer.post(f"/api/tasks/{task_id}/phase", json=body)).status_code == 403
    async with _client(app, "alice") as developer:
        spoofed = await developer.post(
            f"/api/tasks/{task_id}/phase", json={**body, "actor": "mallory"}
        )
        changed = await developer.post(f"/api/tasks/{task_id}/phase", json=body)
        spoofed_artifact = await developer.post(
            f"/api/tasks/{task_id}/artifacts",
            json={
                "phase": "PLAN",
                "kind": "PLAN",
                "payload": _payload(ArtifactKind.PLAN),
                "created_by": "mallory",
            },
        )
        checklist = await developer.post(
            f"/api/tasks/{task_id}/checklists",
            json={"phase": "PLAN", "items": []},
        )
        override = await developer.post(
            f"/api/tasks/{task_id}/phase",
            json={"from_phase": "PLAN", "to_phase": "IMPLEMENTATION"},
        )
    assert spoofed.status_code == 422
    assert changed.status_code == 200
    assert spoofed_artifact.status_code == 422
    assert checklist.json()["readiness"]["eligible_for_auto_progression"] is False
    assert override.status_code == 200
    events = context.storage.get_events(task_id)
    assert [
        event.actor_id for event in events if event.event_type is EventType.WORKFLOW_PHASE_CHANGED
    ] == [
        "alice",
        "alice",
    ]
