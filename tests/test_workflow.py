"""Fast Phase 2 workflow tests using no real Claude calls."""

from pathlib import Path

import pytest

from ai_platform.approval import ApprovalError, approve_task
from ai_platform.events import EventType
from ai_platform.executors.base import ExecutionRequest, ExecutionResult
from ai_platform.graph import run_task_graph
from ai_platform.models import ModelTier, TaskDefinition, TaskStatus, VerificationStatus
from ai_platform.router import ModelRouter
from ai_platform.storage import SQLiteStorage
from ai_platform.task_loader import get_task, load_tasks
from ai_platform.workflow import WorkflowPhase
from ai_platform.workspace import LocalWorkspaceProvider
from tests.fakes import FakeAgentExecutor


def _phase_requests(
    executor: FakeAgentExecutor, phase: WorkflowPhase
) -> list[ExecutionRequest]:
    return [request for request in executor.requests if request.phase is phase]


class FatalFakeAgentExecutor(FakeAgentExecutor):
    """Return a fatal provider error to prove retries are skipped."""

    def execute(self, request):
        self.requests.append(request)
        return ExecutionResult(
            succeeded=False,
            summary="OAuth session expired",
            error="OAuth session expired",
            fatal=True,
        )


def _router() -> ModelRouter:
    return ModelRouter(
        {
            ModelTier.CHEAP: "haiku-test",
            ModelTier.DEFAULT: "sonnet-test",
            ModelTier.STRONG: "opus-test",
        }
    )


def _task(task_id: str) -> TaskDefinition:
    root = Path(__file__).parents[1]
    return get_task(load_tasks(root / "tasks.json"), task_id)


def _run(
    tmp_path: Path,
    task: TaskDefinition,
    executor: FakeAgentExecutor,
) -> tuple[dict, SQLiteStorage, LocalWorkspaceProvider]:
    root = Path(__file__).parents[1]
    storage = SQLiteStorage(tmp_path / "data" / "platform.db")
    storage.initialize()
    storage.create_task(task)
    workspaces = LocalWorkspaceProvider(tmp_path / "workspaces", root / "demo_repo")
    state = run_task_graph(
        task,
        _router(),
        storage,
        workspaces,
        executor,
        tmp_path / "data" / "checkpoints.db",
        verification_timeout_seconds=20,
        max_attempts_per_tier=2,
        actor_id="test",
    )
    return state, storage, workspaces


@pytest.mark.parametrize(
    ("task_id", "expected_tier", "expected_model"),
    [
        ("DEMO-1", ModelTier.DEFAULT, "sonnet-test"),
        ("DEMO-2", ModelTier.DEFAULT, "sonnet-test"),
        ("DEMO-3", ModelTier.DEFAULT, "sonnet-test"),
    ],
)
def test_forwards_routed_model_and_reaches_human_review(
    tmp_path: Path,
    task_id: str,
    expected_tier: ModelTier,
    expected_model: str,
) -> None:
    task = _task(task_id)
    source_file = Path(__file__).parents[1] / "demo_repo" / "app" / "messages.py"
    source_before = source_file.read_bytes()
    executor = FakeAgentExecutor()

    state, storage, _workspaces = _run(tmp_path, task, executor)

    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    implementation_requests = _phase_requests(executor, WorkflowPhase.IMPLEMENTATION)
    assert implementation_requests[0].selection.tier is expected_tier
    assert implementation_requests[0].selection.model == expected_model
    record = storage.get_task(task_id)
    assert record is not None
    assert record.verification_status is VerificationStatus.PASSED
    # Legacy tasks skip Brainstorm/Plan and flow straight through Review to
    # Human Review once Implementation's own gate passes (Phase 3, no human
    # intervention required before Human Review).
    assert record.workflow_phase is WorkflowPhase.HUMAN_REVIEW
    assert len(_phase_requests(executor, WorkflowPhase.REVIEW)) == 1
    assert source_file.read_bytes() == source_before


def test_failed_verification_retries_current_workspace_then_passes(tmp_path: Path) -> None:
    executor = FakeAgentExecutor(fix_on_attempt=2)

    state, storage, _workspaces = _run(tmp_path, _task("DEMO-1"), executor)

    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    implementation_requests = _phase_requests(executor, WorkflowPhase.IMPLEMENTATION)
    assert len(implementation_requests) == 2
    assert implementation_requests[1].previous_failure
    event_types = [event.event_type for event in storage.get_events("DEMO-1")]
    assert event_types.count(EventType.AGENT_STARTED) == 3  # 2 implementation + 1 review
    assert EventType.TEST_FAILED in event_types
    assert EventType.TEST_PASSED in event_types


@pytest.mark.parametrize(
    ("task_id", "fix_tier", "expected_tiers"),
    [
        (
            "DEMO-1",
            ModelTier.STRONG,
            [ModelTier.DEFAULT, ModelTier.DEFAULT, ModelTier.STRONG],
        ),
        (
            "DEMO-2",
            ModelTier.STRONG,
            [ModelTier.DEFAULT, ModelTier.DEFAULT, ModelTier.STRONG],
        ),
    ],
)
def test_repeated_failures_escalate_then_pass(
    tmp_path: Path,
    task_id: str,
    fix_tier: ModelTier,
    expected_tiers: list[ModelTier],
) -> None:
    executor = FakeAgentExecutor(fix_on_tier=fix_tier)

    state, storage, _workspaces = _run(tmp_path, _task(task_id), executor)

    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    implementation_requests = _phase_requests(executor, WorkflowPhase.IMPLEMENTATION)
    assert [request.selection.tier for request in implementation_requests] == expected_tiers
    record = storage.get_task(task_id)
    assert record is not None
    assert record.selected_tier is fix_tier
    events = storage.get_events(task_id)
    selected = [
        event
        for event in events
        if event.event_type is EventType.MODEL_SELECTED
        and event.metadata.get("workflow_phase") == WorkflowPhase.IMPLEMENTATION.value
    ]
    escalated = [event for event in events if event.event_type is EventType.MODEL_ESCALATED]
    assert len(selected) == 1
    assert selected[0].metadata["tier"] == expected_tiers[0].value
    assert len(escalated) == 1
    assert escalated[0].metadata["previous_tier"] == expected_tiers[0].value
    assert escalated[0].metadata["new_tier"] == fix_tier.value
    assert escalated[0].metadata["failed_attempt_count"] == 2
    assert [event.sequence_id for event in events] == sorted(event.sequence_id for event in events)


def test_continuation_resolves_again_and_uses_new_execution_id(
    tmp_path: Path,
) -> None:
    """A gate-stopped Implementation resumes, resolving its model fresh each turn.

    The initial turn never fixes the bug: bounded retries and AUTO escalation
    exhaust, which is a gate stop (WAITING_FOR_HUMAN), not a system FAILED
    (Phase 3, section 17/33). A continuation turn then fixes it and the task
    flows on through Review to Human Review.
    """

    task = _task("DEMO-1")
    initial = FakeAgentExecutor(fix_on_attempt=None)
    initial_state, storage, workspaces = _run(tmp_path, task, initial)
    assert initial_state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    record = storage.get_task(task.id)
    assert record is not None
    assert record.workflow_phase is WorkflowPhase.IMPLEMENTATION
    assert record.verification_status is VerificationStatus.FAILED
    initial_execution_ids = {request.execution_id for request in initial.requests}
    assert initial_execution_ids and len(initial_execution_ids) == 1
    continuation = FakeAgentExecutor()

    state = run_task_graph(
        task,
        _router(),
        storage,
        workspaces,
        continuation,
        tmp_path / "data" / "checkpoints.db",
        verification_timeout_seconds=20,
        max_attempts_per_tier=2,
        execution_id="continuation-execution",
        continuation=True,
    )

    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    continuation_implementation = _phase_requests(continuation, WorkflowPhase.IMPLEMENTATION)
    assert continuation_implementation[0].selection.tier is ModelTier.DEFAULT
    assert continuation_implementation[0].execution_id == "continuation-execution"
    final = storage.get_task(task.id)
    assert final is not None
    assert final.workflow_phase is WorkflowPhase.HUMAN_REVIEW
    events = storage.get_events(task.id)
    implementation_selected = [
        event
        for event in events
        if event.event_type is EventType.MODEL_SELECTED
        and event.metadata.get("workflow_phase") == WorkflowPhase.IMPLEMENTATION.value
    ]
    assert len(implementation_selected) == 2  # one per turn: resolved again, not cached
    assert len([e for e in events if e.event_type is EventType.MODEL_ESCALATED]) == 1


def test_strong_exhaustion_after_two_attempts_is_a_gate_stop_not_a_failure(
    tmp_path: Path,
) -> None:
    """Exhausted bounded retries/escalation are a BUSINESS/GATE STOP (Phase 3,
    section 17/33): the task waits for a human, it is not marked FAILED.
    """

    executor = FakeAgentExecutor(fix_on_attempt=None)

    state, storage, _workspaces = _run(tmp_path, _task("DEMO-3"), executor)

    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    implementation_requests = _phase_requests(executor, WorkflowPhase.IMPLEMENTATION)
    assert len(implementation_requests) == 4
    record = storage.get_task("DEMO-3")
    assert record is not None
    assert record.status is TaskStatus.WAITING_FOR_HUMAN
    assert record.workflow_phase is WorkflowPhase.IMPLEMENTATION
    assert record.verification_status is VerificationStatus.FAILED
    checklists = storage.list_checklist_evaluations("DEMO-3")
    latest = checklists[-1]
    assert "deterministic_verification_passed" in latest.readiness.blocking_failures
    assert not latest.readiness.eligible_for_auto_progression


def test_fatal_agent_failure_skips_verification_and_retry(tmp_path: Path) -> None:
    executor = FatalFakeAgentExecutor()

    state, storage, _workspaces = _run(tmp_path, _task("DEMO-1"), executor)

    assert state["status"] == TaskStatus.FAILED.value
    assert len(executor.requests) == 1
    event_types = [event.event_type for event in storage.get_events("DEMO-1")]
    assert EventType.TEST_STARTED not in event_types


def test_file_events_come_from_actual_git_changes(tmp_path: Path) -> None:
    _state, storage, workspaces = _run(tmp_path, _task("DEMO-1"), FakeAgentExecutor())

    changes = workspaces.get_changed_files("DEMO-1")
    file_events = [
        event
        for event in storage.get_events("DEMO-1")
        if event.event_type is EventType.FILE_CHANGED
    ]
    assert [(change.path, change.change_type) for change in changes] == [
        ("app/messages.py", "modified")
    ]
    assert file_events[-1].metadata["path"] == "app/messages.py"
    assert "Welocme" in workspaces.get_diff("DEMO-1")
    assert "Welcome" in workspaces.get_diff("DEMO-1")


def test_approval_requires_pass_and_preserves_completed_history(tmp_path: Path) -> None:
    task = _task("DEMO-1")
    storage = SQLiteStorage(tmp_path / "not-ready.db")
    storage.initialize()
    storage.create_task(task)
    with pytest.raises(ApprovalError, match="Cannot approve"):
        approve_task(storage, task.id, "reviewer")

    _state, verified_storage, _workspaces = _run(tmp_path / "verified", task, FakeAgentExecutor())
    approve_task(verified_storage, task.id, "reviewer")

    reopened = SQLiteStorage(tmp_path / "verified" / "data" / "platform.db")
    reopened.initialize()
    record = reopened.get_task(task.id)
    assert record is not None
    assert record.status is TaskStatus.COMPLETED
    event_types = [event.event_type for event in reopened.get_events(task.id)]
    assert EventType.HUMAN_APPROVED in event_types
    assert EventType.TASK_COMPLETED in event_types
