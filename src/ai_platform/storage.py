"""Concurrent SQLite persistence for task sessions and append-only events."""

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_platform.events import Event, EventType
from ai_platform.models import (
    ExecutionKind,
    ModelSelection,
    TaskDefinition,
    TaskRecord,
    TaskStatus,
    VerificationStatus,
)

_BUSY_TIMEOUT_MILLISECONDS = 5000


@dataclass(frozen=True, slots=True)
class PauseResult:
    """Result of atomically requesting a human pause."""

    previous_status: TaskStatus
    current_status: TaskStatus
    active_execution: ExecutionKind | None
    deferred: bool


class SQLiteStorage:
    """Persist shared task sessions with short, multi-process-safe transactions."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MILLISECONDS}")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create or migrate the shared database and enable practical concurrency."""

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        with self._connect(immediate=True) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    difficulty TEXT NOT NULL,
                    status TEXT NOT NULL,
                    selected_tier TEXT,
                    selected_model TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    verification_status TEXT NOT NULL DEFAULT 'not_run',
                    workspace_path TEXT,
                    active_execution TEXT,
                    execution_owner TEXT,
                    execution_started_at TEXT,
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._migrate_task_columns(connection)
            self._create_or_migrate_events(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_events_task_sequence
                ON events(task_id, sequence_id)
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS events_are_append_only_update
                BEFORE UPDATE ON events
                BEGIN
                    SELECT RAISE(ABORT, 'events are append-only');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS events_are_append_only_delete
                BEFORE DELETE ON events
                BEGIN
                    SELECT RAISE(ABORT, 'events are append-only');
                END
                """
            )

    def create_task(self, task: TaskDefinition) -> bool:
        """Create initial runtime state, returning whether a row was inserted."""

        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO tasks (
                    task_id, title, difficulty, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task.id, task.title, task.difficulty.value, TaskStatus.READY.value, now, now),
            )
            return cursor.rowcount == 1

    def get_task(self, task_id: str) -> TaskRecord | None:
        """Return runtime state for one task."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._record_from_row(row) if row else None

    def list_tasks(self) -> list[TaskRecord]:
        """Return all task runtime records in stable ID order."""

        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM tasks ORDER BY task_id").fetchall()
        return [self._record_from_row(row) for row in rows]

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        """Update the current lifecycle status of a task."""

        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (status.value, datetime.now(UTC).isoformat(), task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)

    def update_model_selection(self, task_id: str, selection: ModelSelection) -> None:
        """Record the latest selected tier and provider model on the task."""

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET selected_tier = ?, selected_model = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    selection.tier.value,
                    selection.model,
                    datetime.now(UTC).isoformat(),
                    task_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)

    def update_workspace_path(self, task_id: str, workspace_path: Path) -> None:
        """Persist the configured task workspace path."""

        self._update_task_field(task_id, "workspace_path", str(workspace_path))

    def update_attempt(self, task_id: str, attempt: int) -> None:
        """Persist the current implementation attempt number."""

        self._update_task_field(task_id, "attempt", attempt)

    def update_verification_status(self, task_id: str, status: VerificationStatus) -> None:
        """Persist the latest deterministic verification status."""

        self._update_task_field(task_id, "verification_status", status.value)

    def reset_task_runtime(self, task_id: str) -> None:
        """Reset mutable runtime fields while preserving append-only history."""

        with self._connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT active_execution FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["active_execution"]:
                raise RuntimeError(f"{task_id} currently has an active workspace writer")
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, selected_tier = NULL, selected_model = NULL,
                    attempt = 0, verification_status = ?, workspace_path = NULL,
                    active_execution = NULL, execution_owner = NULL,
                    execution_started_at = NULL, pause_requested = 0, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    TaskStatus.READY.value,
                    VerificationStatus.NOT_RUN.value,
                    datetime.now(UTC).isoformat(),
                    task_id,
                ),
            )

    def append_event(self, event: Event) -> Event:
        """Append and return an event. No update or delete event API exists."""

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO events (
                    id, task_id, timestamp, event_type, actor_type, actor_id, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.task_id,
                    event.timestamp.isoformat(),
                    event.event_type.value,
                    event.actor_type.value,
                    event.actor_id,
                    json.dumps(event.metadata, sort_keys=True),
                ),
            )
            sequence_id = int(cursor.lastrowid)
        event.sequence_id = sequence_id
        return event

    def get_events(self, task_id: str) -> list[Event]:
        """Return a task's append-only event stream in insertion order."""

        return self.get_events_after(task_id, 0)

    def get_events_after(self, task_id: str, sequence_id: int) -> list[Event]:
        """Return events newer than an integer cursor, exactly once per cursor advance."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence_id, id, task_id, timestamp, event_type,
                       actor_type, actor_id, metadata_json
                FROM events
                WHERE task_id = ? AND sequence_id > ?
                ORDER BY sequence_id
                """,
                (task_id, sequence_id),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def get_recent_events(
        self,
        task_id: str,
        event_types: Iterable[EventType],
        *,
        limit: int,
    ) -> list[Event]:
        """Return a bounded chronological slice for current agent context."""

        values = [event_type.value for event_type in event_types]
        if not values or limit < 1:
            return []
        placeholders = ", ".join("?" for _ in values)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT sequence_id, id, task_id, timestamp, event_type,
                       actor_type, actor_id, metadata_json
                FROM events
                WHERE task_id = ? AND event_type IN ({placeholders})
                ORDER BY sequence_id DESC
                LIMIT ?
                """,
                (task_id, *values, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in reversed(rows)]

    def try_acquire_execution(
        self,
        task_id: str,
        kind: ExecutionKind,
        owner: str,
        *,
        allowed_statuses: set[TaskStatus],
    ) -> bool:
        """Atomically acquire the one-writer workspace lock for a task."""

        if not allowed_statuses:
            return False
        placeholders = ", ".join("?" for _ in allowed_statuses)
        statuses = sorted(status.value for status in allowed_statuses)
        now = datetime.now(UTC).isoformat()
        with self._connect(immediate=True) as connection:
            cursor = connection.execute(
                f"""
                UPDATE tasks
                SET active_execution = ?, execution_owner = ?, execution_started_at = ?,
                    updated_at = ?
                WHERE task_id = ? AND active_execution IS NULL AND pause_requested = 0
                  AND status IN ({placeholders})
                """,
                (kind.value, owner, now, now, task_id, *statuses),
            )
            return cursor.rowcount == 1

    def release_execution(self, task_id: str, owner: str) -> tuple[TaskStatus, TaskStatus] | None:
        """Release a writer lock and finalize a pause requested during an agent turn."""

        with self._connect(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT status, execution_owner, pause_requested
                FROM tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["execution_owner"] != owner:
                return None
            previous = TaskStatus(row["status"])
            current = TaskStatus.PAUSED_BY_HUMAN if bool(row["pause_requested"]) else previous
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, active_execution = NULL, execution_owner = NULL,
                    execution_started_at = NULL, pause_requested = 0, updated_at = ?
                WHERE task_id = ? AND execution_owner = ?
                """,
                (current.value, datetime.now(UTC).isoformat(), task_id, owner),
            )
            return (previous, current) if previous is not current else None

    def request_pause(self, task_id: str) -> PauseResult:
        """Atomically pause now or set a cooperative request for the active agent."""

        with self._connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT status, active_execution FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            previous = TaskStatus(row["status"])
            active = ExecutionKind(row["active_execution"]) if row["active_execution"] else None
            deferred = active is ExecutionKind.AGENT
            current = previous if deferred else TaskStatus.PAUSED_BY_HUMAN
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, pause_requested = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (current.value, int(deferred), datetime.now(UTC).isoformat(), task_id),
            )
        return PauseResult(previous, current, active, deferred)

    def is_pause_requested(self, task_id: str) -> bool:
        """Return the cross-process cooperative cancellation flag."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT pause_requested FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return bool(row and row["pause_requested"])

    def try_complete_verified_task(self, task_id: str) -> bool:
        """Atomically complete a verified review state if no writer won the race."""

        with self._connect(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?
                WHERE task_id = ? AND status = ? AND verification_status = ?
                  AND active_execution IS NULL
                """,
                (
                    TaskStatus.COMPLETED.value,
                    datetime.now(UTC).isoformat(),
                    task_id,
                    TaskStatus.WAITING_FOR_HUMAN.value,
                    VerificationStatus.PASSED.value,
                ),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            title=row["title"],
            difficulty=row["difficulty"],
            status=row["status"],
            selected_tier=row["selected_tier"],
            selected_model=row["selected_model"],
            attempt=row["attempt"],
            verification_status=row["verification_status"],
            workspace_path=row["workspace_path"],
            active_execution=row["active_execution"],
            execution_owner=row["execution_owner"],
            execution_started_at=row["execution_started_at"],
            pause_requested=bool(row["pause_requested"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Event:
        return Event(
            sequence_id=row["sequence_id"],
            id=row["id"],
            task_id=row["task_id"],
            timestamp=row["timestamp"],
            event_type=row["event_type"],
            actor_type=row["actor_type"],
            actor_id=row["actor_id"],
            metadata=json.loads(row["metadata_json"]),
        )

    def _update_task_field(self, task_id: str, field: str, value: object) -> None:
        allowed_fields = {"workspace_path", "attempt", "verification_status"}
        if field not in allowed_fields:
            raise ValueError(f"Unsupported task field: {field}")
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE tasks SET {field} = ?, updated_at = ? WHERE task_id = ?",
                (value, datetime.now(UTC).isoformat(), task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)

    @staticmethod
    def _migrate_task_columns(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
        migrations = {
            "attempt": "ALTER TABLE tasks ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0",
            "verification_status": (
                "ALTER TABLE tasks ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'not_run'"
            ),
            "workspace_path": "ALTER TABLE tasks ADD COLUMN workspace_path TEXT",
            "active_execution": "ALTER TABLE tasks ADD COLUMN active_execution TEXT",
            "execution_owner": "ALTER TABLE tasks ADD COLUMN execution_owner TEXT",
            "execution_started_at": "ALTER TABLE tasks ADD COLUMN execution_started_at TEXT",
            "pause_requested": (
                "ALTER TABLE tasks ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0"
            ),
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)

    @staticmethod
    def _create_or_migrate_events(connection: sqlite3.Connection) -> None:
        existing = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()
        if existing is None:
            SQLiteStorage._create_events_table(connection, "events")
            return
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)")}
        if "sequence_id" in columns:
            return

        connection.execute("DROP TRIGGER IF EXISTS events_are_append_only_update")
        connection.execute("DROP TRIGGER IF EXISTS events_are_append_only_delete")
        connection.execute("DROP TABLE IF EXISTS events_phase3_migration")
        SQLiteStorage._create_events_table(connection, "events_phase3_migration")
        connection.execute(
            """
            INSERT INTO events_phase3_migration (
                id, task_id, timestamp, event_type, actor_type, actor_id, metadata_json
            )
            SELECT id, task_id, timestamp, event_type, actor_type, actor_id, metadata_json
            FROM events ORDER BY rowid
            """
        )
        connection.execute("DROP TABLE events")
        connection.execute("ALTER TABLE events_phase3_migration RENAME TO events")

    @staticmethod
    def _create_events_table(connection: sqlite3.Connection, table_name: str) -> None:
        if table_name not in {"events", "events_phase3_migration"}:
            raise ValueError("Unexpected events table name")
        connection.execute(
            f"""
            CREATE TABLE {table_name} (
                sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(task_id)
            )
            """
        )
