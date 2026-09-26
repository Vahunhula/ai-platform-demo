"""Phase 4.1 permanent TaskSession removal and isolation tests."""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_platform.api.app import create_app
from ai_platform.auth import AuthenticatedUser, Role
from ai_platform.events import ActorType, Event, EventType
from ai_platform.models import ExecutionKind, MessageStatus, TaskDisposition, TaskStatus
from ai_platform.removal import TaskRemovalConflictError, TaskRemovalService
from ai_platform.workflow import (
    ArtifactKind,
    ChecklistItem,
    ChecklistStatus,
    ReadinessDecision,
    WorkflowPhase,
)
from tests.test_messaging import _client, _context


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _user(role: Role = Role.DEVELOPER) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=f"id-{role.value}",
        username=role.value,
        display_name=role.value.title(),
        role=role,
    )


def _terminal(context, task_id: str, disposition: TaskDisposition) -> None:  # noqa: ANN001
    with context.storage.transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE tasks SET status = ?, disposition = ?, workflow_phase = ?,
                active_execution = NULL, pause_requested = 0
            WHERE task_id = ?
            """,
            (
                TaskStatus.COMPLETED.value,
                disposition.value,
                WorkflowPhase.HUMAN_REVIEW.value,
                task_id,
            ),
        )


def _checkpoint(path: Path, thread_id: str, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS checkpoints (
            thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT, type TEXT,
            checkpoint BLOB, metadata BLOB,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
        );
        CREATE TABLE IF NOT EXISTS writes (
            thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, idx INTEGER NOT NULL,
            channel TEXT NOT NULL, type TEXT, value BLOB,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
        );
        """
    )
    connection.execute(
        "INSERT INTO checkpoints VALUES (?, '', 'cp', NULL, 'bytes', ?, ?)",
        (thread_id, value, value),
    )
    connection.execute(
        "INSERT INTO writes VALUES (?, '', 'cp', 'node', 0, 'channel', 'bytes', ?)",
        (thread_id, value),
    )
    connection.commit()
    connection.close()


def _checkpoint_count(path: Path, thread_id: str) -> tuple[int, int]:
    connection = sqlite3.connect(path)
    result = tuple(
        connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE thread_id = ?", (thread_id,)
        ).fetchone()[0]
        for table in ("checkpoints", "writes")
    )
    connection.close()
    return result  # type: ignore[return-value]


@pytest.mark.anyio
async def test_removal_availability_permission_and_terminal_dispositions(tmp_path: Path) -> None:
    context = _context(tmp_path)
    app = create_app(context)
    async with _client(app) as developer:
        for status in (
            TaskStatus.READY,
            TaskStatus.RUNNING if hasattr(TaskStatus, "RUNNING") else TaskStatus.ANALYZING,
            TaskStatus.PAUSED_BY_HUMAN,
            TaskStatus.WAITING_FOR_HUMAN,
        ):
            context.storage.update_task_status("DEMO-1", status)
            detail = (await developer.get("/api/tasks/DEMO-1")).json()
            assert detail["can_remove"] is False
            assert detail["remove_disabled_reason"]
            response = await developer.delete("/api/tasks/DEMO-1")
            assert response.status_code == 409

        _terminal(context, "DEMO-1", TaskDisposition.CONFIRMED)
        detail = (await developer.get("/api/tasks/DEMO-1")).json()
        assert detail["can_remove"] is True

    async with _client(app, username="read-only", role=Role.VIEWER) as viewer:
        detail = (await viewer.get("/api/tasks/DEMO-1")).json()
        assert detail["can_remove"] is False
        assert detail["remove_disabled_reason"] == (
            "Developer access is required to remove a task."
        )
        response = await viewer.delete("/api/tasks/DEMO-1")
        assert response.status_code == 403

    async with _client(app) as developer:
        assert (await developer.delete("/api/tasks/DEMO-1")).status_code == 204

    with context.storage.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE tasks SET status = ?, workflow_phase = ? WHERE task_id = ?",
            (
                TaskStatus.WAITING_FOR_HUMAN.value,
                WorkflowPhase.HUMAN_REVIEW.value,
                "DEMO-2",
            ),
        )
    async with _client(app) as developer:
        deferred = await developer.post("/api/tasks/DEMO-2/defer")
        assert deferred.status_code == 200
        assert context.storage.get_task("DEMO-2").disposition is TaskDisposition.DEFERRED
        assert (await developer.delete("/api/tasks/DEMO-2")).status_code == 204


@pytest.mark.anyio
async def test_full_cleanup_is_task_scoped_and_seed_task_stays_removed(tmp_path: Path) -> None:
    context = _context(tmp_path)
    for task_id in ("DEMO-1", "DEMO-2"):
        workspace = context.workspaces.create(task_id)
        context.storage.update_workspace_path(task_id, workspace)
        context.storage.append_event(
            Event(
                task_id=task_id,
                event_type=EventType.AGENT_COMPLETED,
                actor_type=ActorType.AGENT,
                actor_id="fake",
                metadata={"session_id": f"fake-session-{task_id}"},
            )
        )
        context.storage.create_workflow_artifact(
            task_id,
            WorkflowPhase.PLAN,
            ArtifactKind.PLAN,
            {"summary": task_id},
            "test",
        )
        item = ChecklistItem(
            key="implementation_steps_complete",
            label="Steps",
            weight=100,
            blocking=True,
            status=ChecklistStatus.PASS,
            evidence="present",
        )
        context.storage.create_checklist_evaluation(
            task_id,
            WorkflowPhase.PLAN,
            [item],
            ReadinessDecision(
                score=100,
                blocking_failures=[],
                blocking_needs_human=[],
                eligible_for_auto_progression=True,
            ),
            "test",
        )
        queued_event = Event(
            task_id=task_id,
            event_type=EventType.HUMAN_MESSAGE,
            actor_type=ActorType.HUMAN,
            actor_id="developer",
            metadata={"message": "done"},
        )
        message, _created = context.storage.enqueue_message(
            queued_event,
            message_id=f"message-{task_id}",
            client_message_id=f"client-{task_id}",
            display_name="Developer",
        )
        context.storage.finish_message(
            message.message_id,
            status=MessageStatus.COMPLETED,
            expected=MessageStatus.QUEUED,
        )
        _checkpoint(context.settings.checkpoint_db_path, task_id, task_id.encode())

    app = create_app(context)
    async with _client(app) as client:
        assert (await client.post("/api/tasks/DEMO-1/presence")).status_code == 200
    _terminal(context, "DEMO-1", TaskDisposition.CONFIRMED)
    events_b = context.storage.get_events("DEMO-2")
    workspace_b = context.workspaces.get_path("DEMO-2")

    TaskRemovalService(
        context.storage, context.workspaces, context.settings.checkpoint_db_path
    ).remove("DEMO-1", _user())

    assert context.storage.get_task("DEMO-1", include_removing=True) is None
    assert not context.workspaces.exists("DEMO-1")
    assert _checkpoint_count(context.settings.checkpoint_db_path, "DEMO-1") == (0, 0)
    with context.storage.transaction() as connection:
        for table in (
            "events",
            "workflow_artifacts",
            "checklist_evaluations",
            "message_queue",
            "task_presence",
            "task_phase_model_preferences",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE task_id = ?", ("DEMO-1",)
            ).fetchone()[0] == 0
        tombstone = connection.execute(
            "SELECT task_id, deleted_by FROM removed_tasks WHERE task_id = 'DEMO-1'"
        ).fetchone()
        assert tuple(tombstone) == ("DEMO-1", "developer")

    assert context.storage.get_task("DEMO-2") is not None
    assert workspace_b.is_dir()
    assert context.storage.get_events("DEMO-2") == events_b
    assert _checkpoint_count(context.settings.checkpoint_db_path, "DEMO-2") == (1, 1)
    restarted = _context(tmp_path)
    assert restarted.storage.get_task("DEMO-1") is None
    assert restarted.storage.get_task("DEMO-2") is not None


def test_writer_queue_and_concurrent_double_remove_fail_closed(tmp_path: Path) -> None:
    context = _context(tmp_path)
    service = TaskRemovalService(
        context.storage, context.workspaces, context.settings.checkpoint_db_path
    )
    _terminal(context, "DEMO-1", TaskDisposition.CONFIRMED)
    assert context.storage.try_acquire_execution(
        "DEMO-1",
        ExecutionKind.AGENT,
        "writer",
        allowed_statuses={TaskStatus.COMPLETED},
    )
    with pytest.raises(TaskRemovalConflictError, match="active workspace writer"):
        service.remove("DEMO-1", _user())
    context.storage.release_execution("DEMO-1", "writer")

    _terminal(context, "DEMO-3", TaskDisposition.DEFERRED)
    queued, _created = context.storage.enqueue_message(
        Event(
            task_id="DEMO-3",
            event_type=EventType.HUMAN_MESSAGE,
            actor_type=ActorType.HUMAN,
            actor_id="developer",
            metadata={"message": "still pending"},
        ),
        message_id="pending-message",
        client_message_id="pending-client",
        display_name="Developer",
    )
    assert queued.status is MessageStatus.QUEUED
    with pytest.raises(TaskRemovalConflictError, match="instructions still queued"):
        service.remove("DEMO-3", _user())

    _terminal(context, "DEMO-2", TaskDisposition.CONFIRMED)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.remove, "DEMO-2", _user()) for _ in range(2)]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result().task_id)
        except Exception as error:  # noqa: BLE001 - asserting deterministic loser category
            outcomes.append(type(error).__name__)
    assert outcomes.count("DEMO-2") >= 1
    assert set(outcomes) <= {"DEMO-2", "TaskNotFoundError", "TaskRemovalConflictError"}


@pytest.mark.anyio
async def test_removal_guard_blocks_messages_claude_commands_and_sse_reconnect(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _terminal(context, "DEMO-1", TaskDisposition.CONFIRMED)
    context.storage.begin_task_removal("DEMO-1", "developer")
    app = create_app(context)
    async with _client(app) as client:
        message = await client.post(
            "/api/tasks/DEMO-1/messages",
            json={"message": "zombie", "client_message_id": "zombie-message-1"},
        )
        command = await client.post(
            "/api/tasks/DEMO-1/claude-commands",
            json={"command_text": "claude/status", "client_command_id": "zombie-command-1"},
        )
        platform_command = await client.post(
            "/api/tasks/DEMO-1/commands",
            json={"command_text": "/status", "client_command_id": "zombie-platform-1"},
        )
        stream = await client.get("/api/tasks/DEMO-1/stream")
    assert message.status_code == 404
    assert command.status_code == 404
    assert platform_command.status_code == 404
    assert stream.status_code == 404
    assert all(
        event.metadata.get("message") != "zombie"
        for event in context.storage.get_events("DEMO-1")
    )
    # A new process/request can idempotently finish external cleanup after a
    # crash that occurred immediately after the durable guard was acquired.
    result = TaskRemovalService(
        context.storage, context.workspaces, context.settings.checkpoint_db_path
    ).remove("DEMO-1", _user())
    assert result.task_id == "DEMO-1"
    async with _client(app) as client:
        assert (await client.get("/api/tasks/DEMO-1")).status_code == 404


@pytest.mark.anyio
async def test_concurrent_confirm_and_defer_have_one_terminal_winner(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with context.storage.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE tasks SET status = ?, workflow_phase = ? WHERE task_id = 'DEMO-1'",
            (TaskStatus.WAITING_FOR_HUMAN.value, WorkflowPhase.HUMAN_REVIEW.value),
        )
    app = create_app(context)
    async with (
        _client(app, username="confirm-user") as confirmer,
        _client(app, username="defer-user") as deferrer,
    ):
        confirmed, deferred = await asyncio.gather(
            confirmer.post("/api/tasks/DEMO-1/approve"),
            deferrer.post("/api/tasks/DEMO-1/defer"),
        )

    assert sorted((confirmed.status_code, deferred.status_code)) == [200, 409]
    record = context.storage.get_task("DEMO-1")
    assert record is not None
    assert record.status is TaskStatus.COMPLETED
    assert record.disposition in {TaskDisposition.CONFIRMED, TaskDisposition.DEFERRED}


@pytest.mark.anyio
async def test_isolated_combined_phase5_smoke(tmp_path: Path) -> None:
    """No production paths and no paid model call: exercise both namespaces and removal."""

    context = _context(tmp_path)
    for task_id in ("DEMO-1", "DEMO-2"):
        workspace = context.workspaces.create(task_id)
        context.storage.update_workspace_path(task_id, workspace)
        _checkpoint(context.settings.checkpoint_db_path, task_id, task_id.encode())
    app = create_app(context)
    async with _client(app) as client:
        platform_status = await client.post(
            "/api/tasks/DEMO-1/commands",
            json={"command_text": "/status", "client_command_id": "smoke-platform-1"},
        )
        claude_help = await client.post(
            "/api/tasks/DEMO-1/claude-commands",
            json={"command_text": "claude/help", "client_command_id": "smoke-claude-help"},
        )
        claude_status = await client.post(
            "/api/tasks/DEMO-1/claude-commands",
            json={"command_text": "claude/status", "client_command_id": "smoke-claude-status"},
        )
        assert platform_status.status_code == 200
        assert claude_help.status_code == 200
        assert claude_status.status_code == 200
        events = (await client.get("/api/tasks/DEMO-1/events")).json()
        assert any(event["metadata"].get("namespace") == "claude" for event in events)

        _terminal(context, "DEMO-1", TaskDisposition.CONFIRMED)
        removed = await client.delete("/api/tasks/DEMO-1")
        assert removed.status_code == 204
        assert (await client.get("/api/tasks/DEMO-1")).status_code == 404
        assert (await client.post("/api/tasks/DEMO-1/presence")).status_code == 404

    assert not context.workspaces.exists("DEMO-1")
    assert _checkpoint_count(context.settings.checkpoint_db_path, "DEMO-1") == (0, 0)
    assert context.workspaces.exists("DEMO-2")
    assert _checkpoint_count(context.settings.checkpoint_db_path, "DEMO-2") == (1, 1)
