"""Fast Phase 2 workflow tests using no real Claude calls."""

from pathlib import Path

import pytest

from ai_platform.approval import ApprovalError, approve_task
from ai_platform.events import EventType
from ai_platform.executors.base import ExecutionResult
from ai_platform.graph import run_task_graph
from ai_platform.models import ModelTier, TaskDefinition, TaskStatus, VerificationStatus
from ai_platform.router import ModelRouter
from ai_platform.storage import SQLiteStorage
from ai_platform.task_loader import get_task, load_tasks
from ai_platform.workspace import LocalWorkspaceProvider
from tests.fakes import FakeAgentExecutor


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
        ("DEMO-1", ModelTier.CHEAP, "haiku-test"),
        ("DEMO-2", ModelTier.DEFAULT, "sonnet-test"),
        ("DEMO-3", ModelTier.STRONG, "opus-test"),
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
    assert executor.requests[0].selection.tier is expected_tier
    assert executor.requests[0].selection.model == expected_model
    record = storage.get_task(task_id)
    assert record is not None
    assert record.verification_status is VerificationStatus.PASSED
    assert source_file.read_bytes() == source_before


def test_failed_verification_retries_current_workspace_then_passes(tmp_path: Path) -> None:
    executor = FakeAgentExecutor(fix_on_attempt=2)

    state, storage, _workspaces = _run(tmp_path, _task("DEMO-1"), executor)

    assert state["status"] == TaskStatus.WAITING_FOR_HUMAN.value
    assert len(executor.requests) == 2
    assert executor.requests[1].previous_failure
    event_types = [event.event_type for event in storage.get_events("DEMO-1")]
    assert event_types.count(EventType.AGENT_STARTED) == 2
    assert EventType.TEST_FAILED in event_types
    assert EventType.TEST_PASSED in event_types


@pytest.mark.parametrize(
    ("task_id", "fix_tier", "expected_tiers"),
    [
        (
            "DEMO-1",
            ModelTier.DEFAULT,
            [ModelTier.CHEAP, ModelTier.CHEAP, ModelTier.DEFAULT],
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
    assert [request.selection.tier for request in executor.requests] == expected_tiers
    record = storage.get_task(task_id)
    assert record is not None
    assert record.selected_tier is fix_tier
    events = storage.get_events(task_id)
    selected = [event for event in events if event.event_type is EventType.MODEL_SELECTED]
    escalated = [event for event in events if event.event_type is EventType.MODEL_ESCALATED]
    assert len(selected) == 1
    assert selected[0].metadata["tier"] == expected_tiers[0].value
    assert len(escalated) == 1
    assert escalated[0].metadata["previous_tier"] == expected_tiers[0].value
    assert escalated[0].metadata["new_tier"] == fix_tier.value
    assert escalated[0].metadata["failed_attempt_count"] == 2
    assert [event.sequence_id for event in events] == sorted(
        event.sequence_id for event in events
    )


def test_continuation_keeps_current_escalated_tier_and_new_execution_id(
    tmp_path: Path,
) -> None:
    task = _task("DEMO-1")
    initial = FakeAgentExecutor(fix_on_tier=ModelTier.DEFAULT)
    _state, storage, workspaces = _run(tmp_path, task, initial)
    initial_execution_ids = {request.execution_id for request in initial.requests}
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
    assert initial_execution_ids and len(initial_execution_ids) == 1
    assert continuation.requests[0].selection.tier is ModelTier.DEFAULT
    assert continuation.requests[0].execution_id == "continuation-execution"
    assert continuation.requests[0].selection.tier is not ModelTier.CHEAP
    model_events = [
        event.event_type
        for event in storage.get_events(task.id)
        if event.event_type in {EventType.MODEL_SELECTED, EventType.MODEL_ESCALATED}
    ]
    assert model_events == [EventType.MODEL_SELECTED, EventType.MODEL_ESCALATED]


def test_strong_exhaustion_after_two_attempts_marks_task_failed(tmp_path: Path) -> None:
    executor = FakeAgentExecutor(fix_on_attempt=None)

    state, storage, _workspaces = _run(tmp_path, _task("DEMO-3"), executor)

    assert state["status"] == TaskStatus.FAILED.value
    assert len(executor.requests) == 2
    record = storage.get_task("DEMO-3")
    assert record is not None
    assert record.status is TaskStatus.FAILED
    assert record.verification_status is VerificationStatus.FAILED


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
