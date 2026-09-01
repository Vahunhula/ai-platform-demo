"""Small SQLite persistence layer for task runtime state and audit events."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ai_platform.events import Event
from ai_platform.models import ModelSelection, TaskDefinition, TaskRecord, TaskStatus


class SQLiteStorage:
    """Persist platform state without introducing an ORM."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create the database and tables when they do not exist."""

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    difficulty TEXT NOT NULL,
                    status TEXT NOT NULL,
                    selected_tier TEXT,
                    selected_model TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );

                CREATE INDEX IF NOT EXISTS idx_events_task_timestamp
                    ON events(task_id, timestamp, id);
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
                (
                    task.id,
                    task.title,
                    task.difficulty.value,
                    TaskStatus.READY.value,
                    now,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def get_task(self, task_id: str) -> TaskRecord | None:
        """Return runtime state for one task."""

        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
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

    def append_event(self, event: Event) -> None:
        """Append an event. There is intentionally no update or delete event API."""

        with self._connect() as connection:
            connection.execute(
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

    def get_events(self, task_id: str) -> list[Event]:
        """Return a task's append-only event stream in chronological order."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, task_id, timestamp, event_type, actor_type, actor_id, metadata_json
                FROM events
                WHERE task_id = ?
                ORDER BY timestamp, id
                """,
                (task_id,),
            ).fetchall()
        return [
            Event(
                id=row["id"],
                task_id=row["task_id"],
                timestamp=row["timestamp"],
                event_type=row["event_type"],
                actor_type=row["actor_type"],
                actor_id=row["actor_id"],
                metadata=json.loads(row["metadata_json"]),
            )
            for row in rows
        ]

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            title=row["title"],
            difficulty=row["difficulty"],
            status=row["status"],
            selected_tier=row["selected_tier"],
            selected_model=row["selected_model"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
