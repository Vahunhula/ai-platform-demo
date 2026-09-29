"""Demo 2.5 Phase 3: the phase-aware LangGraph pipeline, no real Claude calls.

Covers the deterministic scenarios from the Phase 3 spec: the happy path
reaching Human Review in one turn, each read-only phase's gate stopping and
rerunning with feedback, bounded Implementation failure, Review findings at
each severity, read-only enforcement, per-phase model routing, and Human
Review running no agent.
"""

from pathlib import Path

import pytest

from ai_platform.events import ActorType, Event, EventType
from ai_platform.graph import run_task_graph
from ai_platform.models import LogicalModel, ModelTier, TaskStatus, VerificationStatus
from ai_platform.router import ModelRouter
from ai_platform.storage import SQLiteStorage
from ai_platform.task_loader import get_task, load_tasks
from ai_platform.workflow import ArtifactKind, WorkflowPhase
from ai_platform.workspace import LocalWorkspaceProvider
from tests.fakes import (
    CLEAN_BRAINSTORM_PAYLOAD,
    CLEAN_PLAN_PAYLOAD,
    CLEAN_REVIEW_PAYLOAD,
    FakeAgentExecutor,
)

_ROOT = Path(__file__).parents[1]


def _router() -> ModelRouter:
    return ModelRouter(
        {
            ModelTier.CHEAP: "haiku-test",
            ModelTier.DEFAULT: "sonnet-test",
            ModelTier.STRONG: "opus-test",
        }
    )


def _managed_task(tmp_path: Path, task_id: str = "DEMO-1"):
    """A task whose durable workflow phase starts at BRAINSTORM, like a browser-
    created task (task_creation.py), without needing the full repository/HTTP
    machinery -- this exercises the graph's phase pipeline directly.
    """

    task = get_task(load_tasks(_ROOT / "tests" / "fixtures" / "demo_tasks.json"), task_id)
    storage = SQLiteStorage(tmp_path / "data" / "platform.db")
    storage.initialize()
    storage.create_task(task)
    storage.transition_workflow_phase(
        task_id,
        WorkflowPhase.IMPLEMENTATION,
        WorkflowPhase.BRAINSTORM,
        Event(
            task_id=task_id,
            event_type=EventType.WORKFLOW_PHASE_CHANGED,
            actor_type=ActorType.SYSTEM,
            actor_id="test-setup",
            metadata={
                "from_phase": "IMPLEMENTATION",
                "to_phase": "BRAINSTORM",
                "transition_mode": "MANUAL",
            },
        ),
    )
    workspaces = LocalWorkspaceProvider(
        tmp_path / "workspaces", _ROOT / "tests" / "fixtures" / "demo_repo"
    )
    return task, storage, workspaces


def _latest_gate(storage: SQLiteStorage, task_id: str, phase: WorkflowPhase):
    return [g for g in storage.list_checklist_evaluations(task_id) if g.phase is phase][-1]


def _run(task, storage, workspaces, executor, tmp_path: Path, **kwargs):
    return run_task_graph(
        task,
        _router(),
        storage,
        workspaces,
        executor,
        tmp_path / "data" / "checkpoints.db",
        verification_timeout_seconds=20,
        max_attempts_per_tier=2,
        actor_id="test",
        **kwargs,
    )


# ---------------------------------------------------------------- happy path


def test_happy_path_reaches_human_review_in_one_turn_no_human_needed(tmp_path: Path) -> None:
    task, storage, workspaces = _managed_task(tmp_path)
    executor = FakeAgentExecutor()

    state = _run(task, storage, workspaces, executor, tmp_path)

    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    record = storage.get_task(task.id)
    assert record.workflow_phase is WorkflowPhase.HUMAN_REVIEW
    assert record.verification_status is VerificationStatus.PASSED
    phases_called = [request.phase for request in executor.requests]
    assert phases_called == [
        WorkflowPhase.BRAINSTORM,
        WorkflowPhase.PLAN,
        WorkflowPhase.IMPLEMENTATION,
        WorkflowPhase.REVIEW,
    ]
    kinds = {artifact.kind for artifact in storage.list_workflow_artifacts(task.id)}
    assert kinds == {
        ArtifactKind.BRAINSTORM_SUMMARY,
        ArtifactKind.PLAN,
        ArtifactKind.IMPLEMENTATION_SUMMARY,
        ArtifactKind.REVIEW_REPORT,
    }
    gates = storage.list_checklist_evaluations(task.id)
    assert [g.phase for g in gates] == [
        WorkflowPhase.BRAINSTORM,
        WorkflowPhase.PLAN,
        WorkflowPhase.IMPLEMENTATION,
        WorkflowPhase.REVIEW,
    ]
    assert all(g.readiness.eligible_for_auto_progression for g in gates)
    transitions = [
        event
        for event in storage.get_events(task.id)
        if event.event_type is EventType.WORKFLOW_PHASE_CHANGED
        and event.metadata.get("transition_mode") == "AUTOMATIC"
    ]
    assert [(t.metadata["from_phase"], t.metadata["to_phase"]) for t in transitions] == [
        ("BRAINSTORM", "PLAN"),
        ("PLAN", "IMPLEMENTATION"),
        ("IMPLEMENTATION", "REVIEW"),
        ("REVIEW", "HUMAN_REVIEW"),
    ]


# ---------------------------------------------------------------- brainstorm gate


def test_brainstorm_blocking_question_waits_then_v2_progresses(tmp_path: Path) -> None:
    task, storage, workspaces = _managed_task(tmp_path)
    blocked_payload = {**CLEAN_BRAINSTORM_PAYLOAD, "questions": ["Which approach should we use?"]}
    executor = FakeAgentExecutor(brainstorm_payload=blocked_payload)

    state = _run(task, storage, workspaces, executor, tmp_path)

    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    record = storage.get_task(task.id)
    assert record.workflow_phase is WorkflowPhase.BRAINSTORM
    gate = storage.list_checklist_evaluations(task.id)[-1]
    assert "blocking_questions_resolved" in gate.readiness.blocking_needs_human
    assert not gate.readiness.eligible_for_auto_progression
    v1 = storage.list_workflow_artifacts(task.id)
    assert len(v1) == 1 and v1[0].version == 1

    # Human feedback arrives; rerun resolves it and creates v2, then progresses.
    resolved = FakeAgentExecutor()
    state2 = _run(
        task,
        storage,
        workspaces,
        resolved,
        tmp_path,
        continuation=True,
        human_messages=["dev: Use approach A."],
    )

    assert state2["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    assert storage.get_task(task.id).workflow_phase is WorkflowPhase.HUMAN_REVIEW
    brainstorm_versions = sorted(
        artifact.version
        for artifact in storage.list_workflow_artifacts(task.id)
        if artifact.kind is ArtifactKind.BRAINSTORM_SUMMARY
    )
    assert brainstorm_versions == [1, 2]
    assert resolved.requests[0].human_messages == ["dev: Use approach A."]


# ---------------------------------------------------------------- plan gate


def test_plan_open_questions_wait_then_progress(tmp_path: Path) -> None:
    task, storage, workspaces = _managed_task(tmp_path)
    blocked_payload = {**CLEAN_PLAN_PAYLOAD, "open_questions": ["Confirm the target module."]}
    executor = FakeAgentExecutor(plan_payload=blocked_payload)

    state = _run(task, storage, workspaces, executor, tmp_path)

    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    assert storage.get_task(task.id).workflow_phase is WorkflowPhase.PLAN
    gate = _latest_gate(storage, task.id, WorkflowPhase.PLAN)
    assert "open_questions_resolved" in gate.readiness.blocking_needs_human

    resolved = FakeAgentExecutor()
    state2 = _run(
        task,
        storage,
        workspaces,
        resolved,
        tmp_path,
        continuation=True,
        human_messages=["dev: Target app/messages.py."],
    )
    assert state2["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    assert storage.get_task(task.id).workflow_phase is WorkflowPhase.HUMAN_REVIEW
    # Only Plan reran (Brainstorm's v1 from the first turn remains current).
    assert (
        len([a for a in storage.list_workflow_artifacts(task.id) if a.kind is ArtifactKind.PLAN])
        == 2
    )


# ---------------------------------------------------------------- review gate


@pytest.mark.parametrize(
    ("findings_field", "expected_key"),
    [
        ("critical_findings", "no_critical_findings"),
        ("major_findings", "no_major_blocking_findings"),
    ],
)
def test_review_blocking_finding_waits_at_review(
    tmp_path: Path, findings_field: str, expected_key: str
) -> None:
    task, storage, workspaces = _managed_task(tmp_path)
    payload = {**CLEAN_REVIEW_PAYLOAD, findings_field: ["Something is wrong."]}
    executor = FakeAgentExecutor(review_payload=payload)

    state = _run(task, storage, workspaces, executor, tmp_path)

    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    assert storage.get_task(task.id).workflow_phase is WorkflowPhase.REVIEW
    gate = _latest_gate(storage, task.id, WorkflowPhase.REVIEW)
    assert expected_key in gate.readiness.blocking_failures
    assert not gate.readiness.eligible_for_auto_progression


def test_review_single_minor_finding_still_reaches_human_review(tmp_path: Path) -> None:
    """One non-blocking minor finding: score 98, still eligible (section 45)."""

    task, storage, workspaces = _managed_task(tmp_path)
    payload = {**CLEAN_REVIEW_PAYLOAD, "minor_findings": ["Consider renaming a variable."]}
    executor = FakeAgentExecutor(review_payload=payload)

    state = _run(task, storage, workspaces, executor, tmp_path)

    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    assert storage.get_task(task.id).workflow_phase is WorkflowPhase.HUMAN_REVIEW
    gate = _latest_gate(storage, task.id, WorkflowPhase.REVIEW)
    assert gate.readiness.score == pytest.approx(98)
    assert gate.readiness.eligible_for_auto_progression
    assert not gate.readiness.blocking_failures


def test_review_is_a_fresh_invocation_with_only_structured_upstream_context(
    tmp_path: Path,
) -> None:
    """Review never receives the Implementation agent's own conversation."""

    task, storage, workspaces = _managed_task(tmp_path)
    executor = FakeAgentExecutor()

    _run(task, storage, workspaces, executor, tmp_path)

    review_request = next(r for r in executor.requests if r.phase is WorkflowPhase.REVIEW)
    assert set(review_request.upstream_artifacts) == {
        "BRAINSTORM_SUMMARY",
        "PLAN",
        "IMPLEMENTATION_SUMMARY",
    }
    assert review_request.recent_agent_messages == []
    assert review_request.continuation is False  # not a continuation of Implementation's turn


# ---------------------------------------------------------------- read-only enforcement


def test_unexpected_mutation_during_read_only_phase_fails_the_gate_closed(
    tmp_path: Path,
) -> None:
    task, storage, workspaces = _managed_task(tmp_path)
    executor = FakeAgentExecutor(mutate_during_read_only=True)

    state = _run(task, storage, workspaces, executor, tmp_path)

    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    assert storage.get_task(task.id).workflow_phase is WorkflowPhase.BRAINSTORM
    gate = storage.list_checklist_evaluations(task.id)[-1]
    assert not gate.readiness.eligible_for_auto_progression
    assert all(item.status.value == "FAIL" for item in gate.items)
    # No artifact is trusted from a phase that mutated the workspace.
    assert storage.list_workflow_artifacts(task.id) == []
    violation_events = [
        event
        for event in storage.get_events(task.id)
        if event.event_type is EventType.WORKFLOW_PHASE_GATE_EVALUATED and "error" in event.metadata
    ]
    assert violation_events


# ---------------------------------------------------------------- model routing


def test_each_phase_resolves_its_own_configured_model(tmp_path: Path) -> None:
    task, storage, workspaces = _managed_task(tmp_path)
    storage.set_phase_model_preference(
        task.id, WorkflowPhase.PLAN, LogicalModel.CLAUDE_OPUS, "dev", "Dev"
    )
    executor = FakeAgentExecutor()

    _run(task, storage, workspaces, executor, tmp_path)

    by_phase = {request.phase: request.selection for request in executor.requests}
    assert by_phase[WorkflowPhase.BRAINSTORM].tier is ModelTier.DEFAULT  # AUTO -> Sonnet
    assert by_phase[WorkflowPhase.PLAN].tier is ModelTier.STRONG  # explicit Opus override
    assert by_phase[WorkflowPhase.IMPLEMENTATION].tier is ModelTier.DEFAULT  # AUTO -> Sonnet
    assert by_phase[WorkflowPhase.REVIEW].tier is ModelTier.STRONG  # AUTO -> Opus


# ---------------------------------------------------------------- human review


def test_human_review_entry_runs_no_agent(tmp_path: Path) -> None:
    task, storage, workspaces = _managed_task(tmp_path)
    workspace = workspaces.create(task.id)
    storage.update_workspace_path(task.id, workspace)
    storage.transition_workflow_phase(
        task.id,
        WorkflowPhase.BRAINSTORM,
        WorkflowPhase.HUMAN_REVIEW,
        Event(
            task_id=task.id,
            event_type=EventType.WORKFLOW_PHASE_CHANGED,
            actor_type=ActorType.SYSTEM,
            actor_id="test-setup",
            metadata={
                "from_phase": "BRAINSTORM",
                "to_phase": "HUMAN_REVIEW",
                "transition_mode": "MANUAL",
            },
        ),
    )
    executor = FakeAgentExecutor()

    state = _run(task, storage, workspaces, executor, tmp_path, continuation=True)

    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    assert executor.requests == []
