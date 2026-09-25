"""Permanent, guarded removal of one terminal TaskSession and its owned resources."""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ai_platform.auth import AuthenticatedUser
from ai_platform.events import EventType
from ai_platform.models import TaskDisposition, TaskRecord, TaskStatus
from ai_platform.sessions import TaskNotFoundError
from ai_platform.storage import SQLiteStorage
from ai_platform.workspace import WorkspaceProvider


class TaskRemovalConflictError(RuntimeError):
    """A task exists but cannot be safely removed now."""


class TaskRemovalPermissionError(RuntimeError):
    """The current user may not permanently remove tasks."""


@dataclass(frozen=True, slots=True)
class RemovalAvailability:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class TaskRemovalResult:
    task_id: str
    disposition: TaskDisposition


class LangGraphCheckpointStore:
    """Delete one LangGraph thread without touching any other checkpoint thread."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def delete_thread(self, thread_id: str) -> None:
        if not self.path.exists():
            return
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("BEGIN IMMEDIATE")
            existing = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            # Fixed identifiers only. The installed SqliteSaver uses `writes`
            # and `checkpoints`; the other two names cover its known split-table
            # schema without ever accepting a browser- or database-supplied name.
            for table in ("writes", "checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                if table in existing:
                    connection.execute(
                        f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,)
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class ClaudeSessionCleaner:
    """Remove only provider transcript files identified by this task's own events."""

    @staticmethod
    def remove(session_ids: set[str], workspace: Path) -> None:
        if not session_ids:
            return
        try:
            from claude_agent_sdk import delete_session  # noqa: PLC0415
        except ImportError:
            # Platform-held IDs are still deleted with events below. There is no
            # remote Claude session resource that this integration creates.
            return
        for session_id in sorted(session_ids):
            try:
                UUID(session_id)
            except ValueError:
                continue
            with suppress(FileNotFoundError):
                delete_session(session_id, directory=str(workspace))


class TaskRemovalService:
    """Own the validation, exclusion, external cleanup, and transactional deletion."""

    def __init__(
        self,
        storage: SQLiteStorage,
        workspaces: WorkspaceProvider,
        checkpoint_path: Path,
    ) -> None:
        self.storage = storage
        self.workspaces = workspaces
        self.checkpoints = LangGraphCheckpointStore(checkpoint_path)

    def availability(
        self, record: TaskRecord, user: AuthenticatedUser
    ) -> RemovalAvailability:
        if not user.role.can_modify_tasks:
            return RemovalAvailability(False, "Developer access is required to remove a task.")
        if record.disposition not in {TaskDisposition.CONFIRMED, TaskDisposition.DEFERRED}:
            return RemovalAvailability(False, "Only CONFIRMED or DEFERRED tasks can be removed.")
        if record.status is not TaskStatus.COMPLETED:
            return RemovalAvailability(False, "Terminal task disposition is inconsistent.")
        if record.active_execution is not None:
            return RemovalAvailability(False, "Task has an active workspace writer.")
        if self.storage.pending_message_count(record.task_id):
            return RemovalAvailability(False, "Task has accepted human instructions still queued.")
        if record.removal_started_at is not None:
            return RemovalAvailability(False, "Task removal is already in progress.")
        return RemovalAvailability(True)

    def remove(self, task_id: str, user: AuthenticatedUser) -> TaskRemovalResult:
        if not user.role.can_modify_tasks:
            raise TaskRemovalPermissionError("Developer access is required to remove a task.")
        current = self.storage.get_task(task_id, include_removing=True)
        if current is None:
            raise TaskNotFoundError(f"No runtime state exists for {task_id}")
        if current.removal_started_at is None:
            availability = self.availability(current, user)
            if not availability.allowed:
                raise TaskRemovalConflictError(availability.reason or "Task cannot be removed.")
            try:
                guarded = self.storage.begin_task_removal(current.task_id, user.username)
            except (RuntimeError, ValueError) as error:
                raise TaskRemovalConflictError(str(error)) from error
            if guarded is None:
                raise TaskNotFoundError(f"No runtime state exists for {task_id}")
        else:
            # External cleanup is intentionally idempotent. A developer retry may
            # finish a removal left guarded by a process crash.
            guarded = current

        workspace = self.workspaces.get_path(guarded.task_id).resolve()
        session_ids = {
            str(event.metadata["session_id"])
            for event in self.storage.get_events(guarded.task_id)
            if event.event_type in {EventType.AGENT_COMPLETED, EventType.AGENT_FAILED}
            and event.metadata.get("session_id")
        }
        ClaudeSessionCleaner.remove(session_ids, workspace)
        self.workspaces.destroy(guarded.task_id)
        self.checkpoints.delete_thread(guarded.task_id)
        if (
            not self.storage.delete_task_metadata(guarded.task_id)
            and self.storage.get_task(guarded.task_id, include_removing=True) is not None
        ):
            raise TaskRemovalConflictError("Task removal guard was lost.")
        assert guarded.disposition is not None
        return TaskRemovalResult(guarded.task_id, guarded.disposition)
