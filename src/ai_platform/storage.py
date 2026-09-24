"""Concurrent SQLite persistence for task sessions and append-only events."""

import json
import sqlite3
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep

from ai_platform.events import ActorType, Event, EventType
from ai_platform.models import (
    ExecutionKind,
    LogicalModel,
    MessageStatus,
    ModelSelection,
    PhaseModelPreference,
    QueuedMessage,
    TaskDefinition,
    TaskRecord,
    TaskStatus,
    VerificationConfig,
    VerificationStatus,
)
from ai_platform.workflow import (
    ArtifactKind,
    ChecklistEvaluation,
    ChecklistItem,
    ReadinessDecision,
    WorkflowArtifact,
    WorkflowPhase,
)

_BUSY_TIMEOUT_MILLISECONDS = 5000


def ensure_group_writable_sqlite_files(db_path: Path) -> None:
    """Keep a shared SQLite database and its transient sidecars group writable."""

    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            continue
        required = stat.S_IRGRP | stat.S_IWGRP
        if mode & required != required:
            path.chmod(mode | required)


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
        ensure_group_writable_sqlite_files(self.db_path)
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
            ensure_group_writable_sqlite_files(self.db_path)
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Open one short transaction on the shared database (used by companion stores)."""

        with self._connect(immediate=immediate) as connection:
            yield connection

    def initialize(self) -> None:
        """Create or migrate the shared database and enable practical concurrency."""

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            deadline = monotonic() + (_BUSY_TIMEOUT_MILLISECONDS / 1000)
            while True:
                try:
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.execute("PRAGMA synchronous = NORMAL")
                    break
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error).lower() or monotonic() >= deadline:
                        raise
                    connection.rollback()
                    sleep(0.05)
        with self._connect(immediate=True) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    difficulty TEXT NOT NULL,
                    status TEXT NOT NULL,
                    workflow_phase TEXT NOT NULL DEFAULT 'BRAINSTORM',
                    default_model_selection TEXT NOT NULL DEFAULT 'AUTO',
                    selected_tier TEXT,
                    selected_model TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    verification_status TEXT NOT NULL DEFAULT 'not_run',
                    workspace_path TEXT,
                    active_execution TEXT,
                    execution_owner TEXT,
                    execution_id TEXT,
                    execution_actor_id TEXT,
                    execution_pid INTEGER,
                    execution_hostname TEXT,
                    execution_started_at TEXT,
                    execution_heartbeat_at TEXT,
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._create_or_migrate_events(connection)
            self._migrate_task_columns(connection)
            self._create_repository_table(connection)
            self._create_workflow_tables(connection)
            self._migrate_workflow_artifact_provenance(connection)
            self._create_model_routing_table(connection)
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
            # Demo 2: delivery state for browser messages. Message content is never
            # stored here; it is the referenced HUMAN_MESSAGE event.
            self._create_or_migrate_message_queue(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_message_queue_task_status
                ON message_queue(task_id, status, event_sequence_id)
                """
            )
            self._create_auth_tables(connection)

    def create_managed_task(self, record: TaskRecord, events: list[Event]) -> None:
        """Atomically persist one eagerly provisioned task and its audit history."""

        with self._connect(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, title, description, difficulty, status, workflow_phase,
                    default_model_selection, workspace_path,
                    repository_id, base_branch, assignee_user_id, jira_key, created_by,
                    acceptance_criteria_json, verification_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.task_id,
                    record.title,
                    record.description,
                    record.difficulty.value,
                    record.status.value,
                    record.workflow_phase.value,
                    record.default_model_selection.value,
                    record.workspace_path,
                    record.repository_id,
                    record.base_branch,
                    record.assignee_user_id,
                    record.jira_key,
                    record.created_by,
                    json.dumps(record.acceptance_criteria),
                    record.verification.model_dump_json() if record.verification else None,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
            for event in events:
                self._insert_event(connection, event)

    def create_task(self, task: TaskDefinition) -> bool:
        """Create initial runtime state, returning whether a row was inserted."""

        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO tasks (
                    task_id, title, difficulty, status, workflow_phase,
                    default_model_selection, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.title,
                    task.difficulty.value,
                    TaskStatus.READY.value,
                    WorkflowPhase.IMPLEMENTATION.value,
                    LogicalModel.AUTO.value,
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

    def transition_workflow_phase(
        self,
        task_id: str,
        expected_from: WorkflowPhase,
        target: WorkflowPhase,
        event: Event,
    ) -> bool:
        """Compare-and-set a phase and append its audit event in one transaction."""

        if event.task_id != task_id or event.event_type is not EventType.WORKFLOW_PHASE_CHANGED:
            raise ValueError("Invalid workflow transition event")
        with self._connect(immediate=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(task_id)
            cursor = connection.execute(
                """
                UPDATE tasks SET workflow_phase = ?, updated_at = ?
                WHERE task_id = ? AND workflow_phase = ?
                """,
                (
                    target.value,
                    event.timestamp.isoformat(),
                    task_id,
                    expected_from.value,
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._insert_event(connection, event)
            return True

    def create_workflow_artifact(
        self,
        task_id: str,
        phase: WorkflowPhase,
        kind: ArtifactKind,
        payload: dict[str, object],
        created_by: str,
        *,
        created_by_type: ActorType = ActorType.HUMAN,
        execution_id: str | None = None,
        logical_model: str | None = None,
        concrete_model: str | None = None,
        provider: str | None = None,
    ) -> WorkflowArtifact:
        """Append the next artifact version under a serialized SQLite write lock."""

        with self._connect(immediate=True) as connection:
            if (
                connection.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                is None
            ):
                raise KeyError(task_id)
            previous = connection.execute(
                """
                SELECT artifact_id, version FROM workflow_artifacts
                WHERE task_id = ? AND kind = ? ORDER BY version DESC LIMIT 1
                """,
                (task_id, kind.value),
            ).fetchone()
            artifact = WorkflowArtifact(
                task_id=task_id,
                phase=phase,
                kind=kind,
                version=(int(previous["version"]) + 1 if previous else 1),
                payload=payload,
                created_by=created_by,
                supersedes_artifact_id=(previous["artifact_id"] if previous else None),
                created_by_type=created_by_type,
                execution_id=execution_id,
                logical_model=logical_model,
                concrete_model=concrete_model,
                provider=provider,
            )
            connection.execute(
                """
                INSERT INTO workflow_artifacts (
                    artifact_id, task_id, phase, kind, version, payload_json,
                    created_by, created_at, supersedes_artifact_id,
                    created_by_type, execution_id, logical_model, concrete_model, provider
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    task_id,
                    phase.value,
                    kind.value,
                    artifact.version,
                    json.dumps(payload, sort_keys=True),
                    created_by,
                    artifact.created_at.isoformat(),
                    artifact.supersedes_artifact_id,
                    created_by_type.value,
                    execution_id,
                    logical_model,
                    concrete_model,
                    provider,
                ),
            )
        return artifact

    def list_workflow_artifacts(
        self, task_id: str, *, current_only: bool = False
    ) -> list[WorkflowArtifact]:
        """Return artifact history, or the greatest version of each kind."""

        with self._connect() as connection:
            if (
                connection.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                is None
            ):
                raise KeyError(task_id)
            current = (
                "AND a.version = (SELECT MAX(b.version) FROM workflow_artifacts b "
                "WHERE b.task_id = a.task_id AND b.kind = a.kind)"
                if current_only
                else ""
            )
            rows = connection.execute(
                f"""
                SELECT a.* FROM workflow_artifacts a
                WHERE a.task_id = ? {current}
                ORDER BY a.kind, a.version
                """,
                (task_id,),
            ).fetchall()
        return [self._artifact_from_row(row) for row in rows]

    def create_checklist_evaluation(
        self,
        task_id: str,
        phase: WorkflowPhase,
        items: list[ChecklistItem],
        readiness: ReadinessDecision,
        created_by: str,
    ) -> ChecklistEvaluation:
        """Append an immutable readiness snapshot and all exposed evidence."""

        with self._connect(immediate=True) as connection:
            if (
                connection.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                is None
            ):
                raise KeyError(task_id)
            row = connection.execute(
                """
                SELECT COALESCE(MAX(evaluation_number), 0) AS latest
                FROM checklist_evaluations WHERE task_id = ? AND phase = ?
                """,
                (task_id, phase.value),
            ).fetchone()
            evaluation = ChecklistEvaluation(
                task_id=task_id,
                phase=phase,
                evaluation_number=int(row["latest"]) + 1,
                created_by=created_by,
                items=items,
                readiness=readiness,
            )
            connection.execute(
                """
                INSERT INTO checklist_evaluations (
                    evaluation_id, task_id, phase, evaluation_number, created_by,
                    created_at, score, blocking_failures_json,
                    blocking_needs_human_json, eligible_for_auto_progression
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    task_id,
                    phase.value,
                    evaluation.evaluation_number,
                    created_by,
                    evaluation.created_at.isoformat(),
                    readiness.score,
                    json.dumps(readiness.blocking_failures),
                    json.dumps(readiness.blocking_needs_human),
                    int(readiness.eligible_for_auto_progression),
                ),
            )
            for position, item in enumerate(items):
                connection.execute(
                    """
                    INSERT INTO checklist_evaluation_items (
                        evaluation_id, position, key, label, weight, blocking,
                        status, evidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evaluation.evaluation_id,
                        position,
                        item.key,
                        item.label,
                        item.weight,
                        int(item.blocking),
                        item.status.value,
                        item.evidence,
                    ),
                )
        return evaluation

    def list_checklist_evaluations(self, task_id: str) -> list[ChecklistEvaluation]:
        """Return every immutable checklist snapshot with its item evidence."""

        with self._connect() as connection:
            if (
                connection.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                is None
            ):
                raise KeyError(task_id)
            rows = connection.execute(
                """
                SELECT * FROM checklist_evaluations
                WHERE task_id = ? ORDER BY created_at, evaluation_id
                """,
                (task_id,),
            ).fetchall()
            evaluations = []
            for row in rows:
                item_rows = connection.execute(
                    """
                    SELECT * FROM checklist_evaluation_items
                    WHERE evaluation_id = ? ORDER BY position
                    """,
                    (row["evaluation_id"],),
                ).fetchall()
                evaluations.append(self._checklist_from_rows(row, item_rows))
        return evaluations

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

    def get_phase_model_preference(
        self, task_id: str, phase: WorkflowPhase
    ) -> PhaseModelPreference | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM task_phase_model_preferences
                WHERE task_id = ? AND phase = ?
                """,
                (task_id, phase.value),
            ).fetchone()
        return self._phase_model_preference_from_row(row) if row else None

    def list_phase_model_preferences(self, task_id: str) -> list[PhaseModelPreference]:
        with self._connect() as connection:
            if (
                connection.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                is None
            ):
                raise KeyError(task_id)
            rows = connection.execute(
                """
                SELECT * FROM task_phase_model_preferences
                WHERE task_id = ? ORDER BY phase
                """,
                (task_id,),
            ).fetchall()
        return [self._phase_model_preference_from_row(row) for row in rows]

    def set_default_model_selection(
        self,
        task_id: str,
        selection: LogicalModel,
        actor_id: str,
        display_name: str,
    ) -> bool:
        """Atomically update user intent and append one audit event unless unchanged."""

        with self._connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT default_model_selection FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            old = LogicalModel(row["default_model_selection"])
            if old is selection:
                return False
            now = datetime.now(UTC)
            connection.execute(
                """
                UPDATE tasks SET default_model_selection = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (selection.value, now.isoformat(), task_id),
            )
            self._insert_event(
                connection,
                Event(
                    task_id=task_id,
                    timestamp=now,
                    event_type=EventType.TASK_MODEL_DEFAULT_CHANGED,
                    actor_type=ActorType.HUMAN,
                    actor_id=actor_id,
                    metadata={
                        "old_selection": old.value,
                        "new_selection": selection.value,
                        "display_name": display_name,
                    },
                ),
            )
            return True

    def set_phase_model_preference(
        self,
        task_id: str,
        phase: WorkflowPhase,
        selection: LogicalModel,
        actor_id: str,
        display_name: str,
    ) -> bool:
        """Atomically upsert one override and its audit history unless unchanged."""

        with self._connect(immediate=True) as connection:
            if (
                connection.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                is None
            ):
                raise KeyError(task_id)
            row = connection.execute(
                """
                SELECT model_selection FROM task_phase_model_preferences
                WHERE task_id = ? AND phase = ?
                """,
                (task_id, phase.value),
            ).fetchone()
            old = LogicalModel(row["model_selection"]) if row else None
            if old is selection:
                return False
            now = datetime.now(UTC)
            connection.execute(
                """
                INSERT INTO task_phase_model_preferences (
                    task_id, phase, model_selection, updated_by, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id, phase) DO UPDATE SET
                    model_selection = excluded.model_selection,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (task_id, phase.value, selection.value, actor_id, now.isoformat()),
            )
            self._insert_event(
                connection,
                Event(
                    task_id=task_id,
                    timestamp=now,
                    event_type=EventType.PHASE_MODEL_OVERRIDE_CHANGED,
                    actor_type=ActorType.HUMAN,
                    actor_id=actor_id,
                    metadata={
                        "phase": phase.value,
                        "old_selection": old.value if old else None,
                        "new_selection": selection.value,
                        "display_name": display_name,
                    },
                ),
            )
            return True

    def update_workspace_path(self, task_id: str, workspace_path: Path) -> None:
        """Persist the configured task workspace path."""

        self._update_task_field(task_id, "workspace_path", str(workspace_path))

    def update_attempt(self, task_id: str, attempt: int) -> None:
        """Persist the current implementation attempt number."""

        self._update_task_field(task_id, "attempt", attempt)

    def update_verification_status(self, task_id: str, status: VerificationStatus) -> None:
        """Persist the latest deterministic verification status."""

        self._update_task_field(task_id, "verification_status", status.value)

    def reset_task_runtime(self, task_id: str, owner: str | None = None) -> None:
        """Reset mutable runtime fields while preserving append-only history.

        With ``owner``, the caller holds the task's writer lock for the reset; the
        reset and the lock release happen in this one atomic update.
        """

        with self._connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT active_execution, execution_owner FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["active_execution"] and (owner is None or row["execution_owner"] != owner):
                raise RuntimeError(f"{task_id} currently has an active workspace writer")
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, selected_tier = NULL, selected_model = NULL,
                    attempt = 0, verification_status = ?, workspace_path = NULL,
                    active_execution = NULL, execution_owner = NULL,
                    execution_id = NULL, execution_actor_id = NULL,
                    execution_pid = NULL, execution_hostname = NULL,
                    execution_started_at = NULL, execution_heartbeat_at = NULL,
                    pause_requested = 0, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    TaskStatus.READY.value,
                    VerificationStatus.NOT_RUN.value,
                    datetime.now(UTC).isoformat(),
                    task_id,
                ),
            )

    def reset_managed_task_runtime(self, task_id: str, workspace_path: Path, owner: str) -> None:
        """Reset a registered task while retaining its freshly provisioned workspace."""

        with self._connect(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = ?, selected_tier = NULL, selected_model = NULL,
                    attempt = 0, verification_status = ?, workspace_path = ?,
                    active_execution = NULL, execution_owner = NULL, execution_id = NULL,
                    execution_actor_id = NULL, execution_pid = NULL,
                    execution_hostname = NULL, execution_started_at = NULL,
                    execution_heartbeat_at = NULL, pause_requested = 0, updated_at = ?
                WHERE task_id = ? AND execution_owner = ?
                """,
                (
                    TaskStatus.READY.value,
                    VerificationStatus.NOT_RUN.value,
                    str(workspace_path),
                    datetime.now(UTC).isoformat(),
                    task_id,
                    owner,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"{task_id} lost its reset workspace lock")

    def append_event(self, event: Event) -> Event:
        """Append and return an event. No update or delete event API exists."""

        with self._connect() as connection:
            self._insert_event(connection, event)
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
        through_sequence_id: int | None = None,
    ) -> list[Event]:
        """Return a bounded chronological slice for current agent context."""

        values = [event_type.value for event_type in event_types]
        if not values or limit < 1:
            return []
        placeholders = ", ".join("?" for _ in values)
        cutoff = "" if through_sequence_id is None else "AND sequence_id <= ?"
        cutoff_args = () if through_sequence_id is None else (through_sequence_id,)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT sequence_id, id, task_id, timestamp, event_type,
                       actor_type, actor_id, metadata_json
                FROM events
                WHERE task_id = ? AND event_type IN ({placeholders}) {cutoff}
                ORDER BY sequence_id DESC
                LIMIT ?
                """,
                (task_id, *values, *cutoff_args, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in reversed(rows)]

    def try_acquire_execution(
        self,
        task_id: str,
        kind: ExecutionKind,
        owner: str,
        *,
        allowed_statuses: set[TaskStatus],
        execution_id: str | None = None,
        actor_id: str | None = None,
        process_id: int | None = None,
        hostname: str | None = None,
        acquired_at: datetime | None = None,
    ) -> bool:
        """Atomically acquire the one-writer workspace lock for a task."""

        if not allowed_statuses:
            return False
        placeholders = ", ".join("?" for _ in allowed_statuses)
        statuses = sorted(status.value for status in allowed_statuses)
        now = (acquired_at or datetime.now(UTC)).isoformat()
        with self._connect(immediate=True) as connection:
            cursor = connection.execute(
                f"""
                UPDATE tasks
                SET active_execution = ?, execution_owner = ?, execution_id = ?,
                    execution_actor_id = ?, execution_pid = ?, execution_hostname = ?,
                    execution_started_at = ?, execution_heartbeat_at = ?, updated_at = ?
                WHERE task_id = ? AND active_execution IS NULL AND pause_requested = 0
                  AND status IN ({placeholders})
                """,
                (
                    kind.value,
                    owner,
                    execution_id or owner,
                    actor_id or owner,
                    process_id,
                    hostname,
                    now,
                    now,
                    now,
                    task_id,
                    *statuses,
                ),
            )
            return cursor.rowcount == 1

    def heartbeat_execution(
        self,
        task_id: str,
        owner: str,
        *,
        heartbeat_at: datetime | None = None,
    ) -> bool:
        """Refresh a lock lease only when its opaque owner token still matches."""

        heartbeat = (heartbeat_at or datetime.now(UTC)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET execution_heartbeat_at = ?, updated_at = ?
                WHERE task_id = ? AND execution_owner = ?
                """,
                (heartbeat, heartbeat, task_id, owner),
            )
            return cursor.rowcount == 1

    def recover_execution(
        self,
        task_id: str,
        expected_owner: str,
        expected_heartbeat: datetime | None,
        *,
        audit_event: Event,
    ) -> tuple[TaskStatus, TaskStatus] | None:
        """Atomically clear a stale lock, pause, and append its audit events."""

        expected = expected_heartbeat.isoformat() if expected_heartbeat else None
        with self._connect(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT status FROM tasks
                WHERE task_id = ? AND execution_owner = ?
                  AND execution_heartbeat_at IS ?
                """,
                (task_id, expected_owner, expected),
            ).fetchone()
            if row is None:
                return None
            previous = TaskStatus(row["status"])
            current = TaskStatus.PAUSED_BY_HUMAN
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = ?, active_execution = NULL, execution_owner = NULL,
                    execution_id = NULL, execution_actor_id = NULL,
                    execution_pid = NULL, execution_hostname = NULL,
                    execution_started_at = NULL, execution_heartbeat_at = NULL,
                    pause_requested = 0, updated_at = ?
                WHERE task_id = ? AND execution_owner = ?
                  AND execution_heartbeat_at IS ?
                """,
                (
                    current.value,
                    datetime.now(UTC).isoformat(),
                    task_id,
                    expected_owner,
                    expected,
                ),
            )
            if cursor.rowcount != 1:
                return None
            if audit_event.task_id != task_id:
                raise ValueError("Recovery audit event belongs to a different task")
            self._insert_event(connection, audit_event)
            if previous is not current:
                self._insert_event(
                    connection,
                    Event(
                        task_id=task_id,
                        event_type=EventType.STATUS_CHANGED,
                        actor_type=ActorType.SYSTEM,
                        actor_id="execution-lock-manager",
                        metadata={
                            "from": previous.value,
                            "to": current.value,
                            "reason": (
                                "Previous execution appears to have terminated unexpectedly"
                            ),
                        },
                    ),
                )
            return (previous, current)

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
                    execution_id = NULL, execution_actor_id = NULL,
                    execution_pid = NULL, execution_hostname = NULL,
                    execution_started_at = NULL, execution_heartbeat_at = NULL,
                    pause_requested = 0, updated_at = ?
                WHERE task_id = ? AND execution_owner = ?
                """,
                (current.value, datetime.now(UTC).isoformat(), task_id, owner),
            )
            return (previous, current) if previous is not current else None

    def request_pause(self, task_id: str) -> PauseResult | None:
        """Atomically pause now or set a cooperative request for the active agent.

        Returns ``None`` (and changes nothing) when a concurrent request already
        paused the task or already requested the pause.
        """

        with self._connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT status, active_execution, pause_requested FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            previous = TaskStatus(row["status"])
            active = ExecutionKind(row["active_execution"]) if row["active_execution"] else None
            deferred = active is ExecutionKind.AGENT
            if bool(row["pause_requested"]) or (
                previous is TaskStatus.PAUSED_BY_HUMAN and not deferred
            ):
                return None
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
                  AND NOT EXISTS (
                      SELECT 1 FROM message_queue
                      WHERE message_queue.task_id = tasks.task_id AND status IN (?, ?)
                  )
                """,
                (
                    TaskStatus.COMPLETED.value,
                    datetime.now(UTC).isoformat(),
                    task_id,
                    TaskStatus.WAITING_FOR_HUMAN.value,
                    VerificationStatus.PASSED.value,
                    MessageStatus.QUEUED.value,
                    MessageStatus.RUNNING.value,
                ),
            )
            return cursor.rowcount == 1

    def pending_message_count(self, task_id: str) -> int:
        """Return accepted browser instructions not yet answered (QUEUED or RUNNING)."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM message_queue WHERE task_id = ? AND status IN (?, ?)",
                (task_id, MessageStatus.QUEUED.value, MessageStatus.RUNNING.value),
            ).fetchone()
        return int(row[0])

    def get_event(self, task_id: str, sequence_id: int) -> Event | None:
        """Return one event of a task by its durable sequence number."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT sequence_id, id, task_id, timestamp, event_type,
                       actor_type, actor_id, metadata_json
                FROM events WHERE task_id = ? AND sequence_id = ?
                """,
                (task_id, sequence_id),
            ).fetchone()
        return self._event_from_row(row) if row else None

    def execution_recorded(self, task_id: str, execution_id: str) -> bool:
        """Return whether an execution ID holds the task lock or appears in its history."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM tasks WHERE task_id = ? AND execution_id = ?
                UNION ALL
                SELECT 1 FROM events
                WHERE task_id = ? AND json_extract(metadata_json, '$.execution_id') = ?
                LIMIT 1
                """,
                (task_id, execution_id, task_id, execution_id),
            ).fetchone()
        return row is not None

    def enqueue_message(
        self,
        event: Event,
        *,
        message_id: str,
        client_message_id: str,
        display_name: str,
        accepting_statuses: Iterable[TaskStatus] | None = None,
    ) -> tuple[QueuedMessage, bool] | None:
        """Append a HUMAN_MESSAGE and its queue entry atomically, once per client key.

        The key is scoped to the author: (task_id, actor_id, client_message_id).
        Returns the stored entry and whether this call created it; a retry returns
        the original entry and appends nothing. Returns ``None`` (nothing written)
        when ``accepting_statuses`` is given and the task is not in one of them —
        checked in the same transaction, so no concurrent approve/reset can slip in.
        """

        with self._connect(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT * FROM message_queue
                WHERE task_id = ? AND actor_id = ? AND client_message_id = ?
                """,
                (event.task_id, event.actor_id, client_message_id),
            ).fetchone()
            if existing is not None:
                return self._queued_message_from_row(existing), False
            if accepting_statuses is not None:
                row = connection.execute(
                    "SELECT status FROM tasks WHERE task_id = ?", (event.task_id,)
                ).fetchone()
                allowed = {status.value for status in accepting_statuses}
                if row is None or row["status"] not in allowed:
                    return None
            self._insert_event(connection, event)
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """
                INSERT INTO message_queue (
                    message_id, task_id, client_message_id, event_sequence_id, actor_id,
                    display_name, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    event.task_id,
                    client_message_id,
                    event.sequence_id,
                    event.actor_id,
                    display_name,
                    MessageStatus.QUEUED.value,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM message_queue WHERE message_id = ?", (message_id,)
            ).fetchone()
        return self._queued_message_from_row(row), True

    def get_queued_message(
        self, task_id: str, actor_id: str, client_message_id: str
    ) -> QueuedMessage | None:
        """Return the delivery record for one author's idempotency key, if it exists."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM message_queue
                WHERE task_id = ? AND actor_id = ? AND client_message_id = ?
                """,
                (task_id, actor_id, client_message_id),
            ).fetchone()
        return self._queued_message_from_row(row) if row else None

    def list_queued_messages(self, task_id: str) -> list[QueuedMessage]:
        """Return every browser message delivery record for a task in FIFO order."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM message_queue WHERE task_id = ? ORDER BY event_sequence_id",
                (task_id,),
            ).fetchall()
        return [self._queued_message_from_row(row) for row in rows]

    def tasks_with_queued_messages(self) -> list[str]:
        """Return task IDs that have at least one QUEUED message."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT task_id FROM message_queue WHERE status = ? ORDER BY task_id",
                (MessageStatus.QUEUED.value,),
            ).fetchall()
        return [row["task_id"] for row in rows]

    def running_messages(self) -> list[QueuedMessage]:
        """Return messages whose agent turn is marked as running."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM message_queue WHERE status = ? ORDER BY event_sequence_id",
                (MessageStatus.RUNNING.value,),
            ).fetchall()
        return [self._queued_message_from_row(row) for row in rows]

    def claim_next_message(self, task_id: str, execution_id: str) -> QueuedMessage | None:
        """Atomically mark the oldest QUEUED message RUNNING if none is running for the task."""

        now = datetime.now(UTC).isoformat()
        with self._connect(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE message_queue
                SET status = ?, execution_id = ?, error = NULL, updated_at = ?
                WHERE message_id = (
                    SELECT message_id FROM message_queue
                    WHERE task_id = ? AND status = ?
                    ORDER BY event_sequence_id LIMIT 1
                )
                AND NOT EXISTS (
                    SELECT 1 FROM message_queue WHERE task_id = ? AND status = ?
                )
                """,
                (
                    MessageStatus.RUNNING.value,
                    execution_id,
                    now,
                    task_id,
                    MessageStatus.QUEUED.value,
                    task_id,
                    MessageStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM message_queue WHERE task_id = ? AND execution_id = ? AND status = ?",
                (task_id, execution_id, MessageStatus.RUNNING.value),
            ).fetchone()
        return self._queued_message_from_row(row)

    def requeue_message(self, message_id: str) -> None:
        """Return a claimed message to the queue when its turn could not start."""

        with self._connect(immediate=True) as connection:
            connection.execute(
                """
                UPDATE message_queue SET status = ?, execution_id = NULL, updated_at = ?
                WHERE message_id = ? AND status = ?
                """,
                (
                    MessageStatus.QUEUED.value,
                    datetime.now(UTC).isoformat(),
                    message_id,
                    MessageStatus.RUNNING.value,
                ),
            )

    def finish_message(
        self,
        message_id: str,
        status: MessageStatus,
        *,
        error: str | None = None,
        expected: MessageStatus = MessageStatus.RUNNING,
    ) -> bool:
        """Move a message to a terminal delivery state exactly once."""

        if status not in {MessageStatus.COMPLETED, MessageStatus.FAILED}:
            raise ValueError("finish_message requires a terminal status")
        with self._connect(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE message_queue SET status = ?, error = ?, updated_at = ?
                WHERE message_id = ? AND status = ?
                """,
                (
                    status.value,
                    error[:2000] if error else None,
                    datetime.now(UTC).isoformat(),
                    message_id,
                    expected.value,
                ),
            )
            return cursor.rowcount == 1

    def message_revision(self, task_id: str) -> str:
        """Return a cheap fingerprint that changes whenever a task's message states change."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total, COALESCE(MAX(updated_at), '') AS latest
                FROM message_queue WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        return f"{row['total']}:{row['latest']}"

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            title=row["title"],
            description=row["description"],
            difficulty=row["difficulty"],
            status=row["status"],
            workflow_phase=row["workflow_phase"],
            default_model_selection=row["default_model_selection"],
            selected_tier=row["selected_tier"],
            selected_model=row["selected_model"],
            attempt=row["attempt"],
            verification_status=row["verification_status"],
            workspace_path=row["workspace_path"],
            repository_id=row["repository_id"],
            base_branch=row["base_branch"],
            assignee_user_id=row["assignee_user_id"],
            jira_key=row["jira_key"],
            created_by=row["created_by"],
            acceptance_criteria=(
                json.loads(row["acceptance_criteria_json"])
                if row["acceptance_criteria_json"]
                else None
            ),
            verification=(
                VerificationConfig.model_validate_json(row["verification_json"])
                if row["verification_json"]
                else None
            ),
            active_execution=row["active_execution"],
            execution_owner=row["execution_owner"],
            execution_id=row["execution_id"],
            execution_actor_id=row["execution_actor_id"],
            execution_pid=row["execution_pid"],
            execution_hostname=row["execution_hostname"],
            execution_started_at=row["execution_started_at"],
            execution_heartbeat_at=row["execution_heartbeat_at"],
            pause_requested=bool(row["pause_requested"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _queued_message_from_row(row: sqlite3.Row) -> QueuedMessage:
        return QueuedMessage(
            message_id=row["message_id"],
            task_id=row["task_id"],
            client_message_id=row["client_message_id"],
            event_sequence_id=row["event_sequence_id"],
            actor_id=row["actor_id"],
            display_name=row["display_name"],
            status=row["status"],
            execution_id=row["execution_id"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _phase_model_preference_from_row(row: sqlite3.Row) -> PhaseModelPreference:
        return PhaseModelPreference(
            task_id=row["task_id"],
            phase=row["phase"],
            model_selection=row["model_selection"],
            updated_by=row["updated_by"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> WorkflowArtifact:
        columns = row.keys()
        return WorkflowArtifact(
            artifact_id=row["artifact_id"],
            task_id=row["task_id"],
            phase=row["phase"],
            kind=row["kind"],
            version=row["version"],
            payload=json.loads(row["payload_json"]),
            created_by=row["created_by"],
            created_at=row["created_at"],
            supersedes_artifact_id=row["supersedes_artifact_id"],
            created_by_type=(
                row["created_by_type"] if "created_by_type" in columns and row["created_by_type"]
                else ActorType.HUMAN
            ),
            execution_id=row["execution_id"] if "execution_id" in columns else None,
            logical_model=row["logical_model"] if "logical_model" in columns else None,
            concrete_model=row["concrete_model"] if "concrete_model" in columns else None,
            provider=row["provider"] if "provider" in columns else None,
        )

    @staticmethod
    def _checklist_from_rows(row: sqlite3.Row, item_rows: list[sqlite3.Row]) -> ChecklistEvaluation:
        return ChecklistEvaluation(
            evaluation_id=row["evaluation_id"],
            task_id=row["task_id"],
            phase=row["phase"],
            evaluation_number=row["evaluation_number"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            items=[
                ChecklistItem(
                    key=item["key"],
                    label=item["label"],
                    weight=item["weight"],
                    blocking=bool(item["blocking"]),
                    status=item["status"],
                    evidence=item["evidence"],
                )
                for item in item_rows
            ],
            readiness=ReadinessDecision(
                score=row["score"],
                blocking_failures=json.loads(row["blocking_failures_json"]),
                blocking_needs_human=json.loads(row["blocking_needs_human_json"]),
                eligible_for_auto_progression=bool(row["eligible_for_auto_progression"]),
            ),
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
    def _insert_event(connection: sqlite3.Connection, event: Event) -> None:
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
        event.sequence_id = int(cursor.lastrowid)

    @staticmethod
    def _migrate_task_columns(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
        migrations = {
            "description": "ALTER TABLE tasks ADD COLUMN description TEXT",
            "attempt": "ALTER TABLE tasks ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0",
            "verification_status": (
                "ALTER TABLE tasks ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'not_run'"
            ),
            "workspace_path": "ALTER TABLE tasks ADD COLUMN workspace_path TEXT",
            "active_execution": "ALTER TABLE tasks ADD COLUMN active_execution TEXT",
            "execution_owner": "ALTER TABLE tasks ADD COLUMN execution_owner TEXT",
            "execution_id": "ALTER TABLE tasks ADD COLUMN execution_id TEXT",
            "execution_actor_id": "ALTER TABLE tasks ADD COLUMN execution_actor_id TEXT",
            "execution_pid": "ALTER TABLE tasks ADD COLUMN execution_pid INTEGER",
            "execution_hostname": "ALTER TABLE tasks ADD COLUMN execution_hostname TEXT",
            "execution_started_at": "ALTER TABLE tasks ADD COLUMN execution_started_at TEXT",
            "execution_heartbeat_at": ("ALTER TABLE tasks ADD COLUMN execution_heartbeat_at TEXT"),
            "pause_requested": (
                "ALTER TABLE tasks ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0"
            ),
            "repository_id": "ALTER TABLE tasks ADD COLUMN repository_id TEXT",
            "base_branch": "ALTER TABLE tasks ADD COLUMN base_branch TEXT",
            "assignee_user_id": "ALTER TABLE tasks ADD COLUMN assignee_user_id TEXT",
            "jira_key": "ALTER TABLE tasks ADD COLUMN jira_key TEXT",
            "created_by": "ALTER TABLE tasks ADD COLUMN created_by TEXT",
            "acceptance_criteria_json": (
                "ALTER TABLE tasks ADD COLUMN acceptance_criteria_json TEXT"
            ),
            "verification_json": "ALTER TABLE tasks ADD COLUMN verification_json TEXT",
            "workflow_phase": "ALTER TABLE tasks ADD COLUMN workflow_phase TEXT",
            "default_model_selection": (
                "ALTER TABLE tasks ADD COLUMN default_model_selection TEXT NOT NULL DEFAULT 'AUTO'"
            ),
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)
        if "workflow_phase" not in columns:
            meaningful_events = (
                "'TASK_STARTED', 'AGENT_STARTED', 'AGENT_COMPLETED', 'AGENT_FAILED', "
                "'TEST_STARTED', 'TEST_PASSED', 'TEST_FAILED'"
            )
            connection.execute(
                f"""
                UPDATE tasks
                SET workflow_phase = CASE
                    WHEN repository_id IS NULL THEN ?
                    WHEN status <> ? OR attempt > 0 OR selected_model IS NOT NULL
                         OR EXISTS (
                             SELECT 1 FROM events
                             WHERE events.task_id = tasks.task_id
                               AND events.event_type IN ({meaningful_events})
                         ) THEN ?
                    ELSE ?
                END
                WHERE workflow_phase IS NULL
                """,
                (
                    WorkflowPhase.IMPLEMENTATION.value,
                    TaskStatus.READY.value,
                    WorkflowPhase.IMPLEMENTATION.value,
                    WorkflowPhase.BRAINSTORM.value,
                ),
            )
        allowed = ", ".join(f"'{phase.value}'" for phase in WorkflowPhase)
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS tasks_workflow_phase_valid_insert
            BEFORE INSERT ON tasks
            WHEN NEW.workflow_phase IS NULL OR NEW.workflow_phase NOT IN ({allowed})
            BEGIN SELECT RAISE(ABORT, 'invalid workflow phase'); END
            """
        )
        model_values = ", ".join(f"'{selection.value}'" for selection in LogicalModel)
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS tasks_default_model_valid_insert
            BEFORE INSERT ON tasks
            WHEN NEW.default_model_selection IS NULL
              OR NEW.default_model_selection NOT IN ({model_values})
            BEGIN SELECT RAISE(ABORT, 'invalid default model selection'); END
            """
        )
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS tasks_default_model_valid_update
            BEFORE UPDATE OF default_model_selection ON tasks
            WHEN NEW.default_model_selection IS NULL
              OR NEW.default_model_selection NOT IN ({model_values})
            BEGIN SELECT RAISE(ABORT, 'invalid default model selection'); END
            """
        )
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS tasks_workflow_phase_valid_update
            BEFORE UPDATE OF workflow_phase ON tasks
            WHEN NEW.workflow_phase IS NULL OR NEW.workflow_phase NOT IN ({allowed})
            BEGIN SELECT RAISE(ABORT, 'invalid workflow phase'); END
            """
        )

    @staticmethod
    def _create_model_routing_table(connection: sqlite3.Connection) -> None:
        phases = ", ".join(
            f"'{phase.value}'" for phase in WorkflowPhase if phase is not WorkflowPhase.HUMAN_REVIEW
        )
        selections = ", ".join(f"'{selection.value}'" for selection in LogicalModel)
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS task_phase_model_preferences (
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                phase TEXT NOT NULL CHECK (phase IN ({phases})),
                model_selection TEXT NOT NULL CHECK (model_selection IN ({selections})),
                updated_by TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (task_id, phase)
            )
            """
        )

    @staticmethod
    def _create_workflow_tables(connection: sqlite3.Connection) -> None:
        phases = ", ".join(f"'{phase.value}'" for phase in WorkflowPhase)
        kinds = ", ".join(f"'{kind.value}'" for kind in ArtifactKind)
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS workflow_artifacts (
                artifact_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                phase TEXT NOT NULL CHECK (phase IN ({phases})),
                kind TEXT NOT NULL CHECK (kind IN ({kinds})),
                version INTEGER NOT NULL CHECK (version >= 1),
                payload_json TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                supersedes_artifact_id TEXT REFERENCES workflow_artifacts(artifact_id),
                CHECK (
                    (kind = 'BRAINSTORM_SUMMARY' AND phase = 'BRAINSTORM') OR
                    (kind = 'PLAN' AND phase = 'PLAN') OR
                    (kind = 'IMPLEMENTATION_SUMMARY' AND phase = 'IMPLEMENTATION') OR
                    (kind = 'REVIEW_REPORT' AND phase = 'REVIEW') OR
                    (kind = 'HUMAN_REVIEW_DECISION' AND phase = 'HUMAN_REVIEW')
                ),
                UNIQUE (task_id, kind, version)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_artifacts_task_kind
            ON workflow_artifacts(task_id, kind, version)
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS checklist_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                phase TEXT NOT NULL CHECK (phase IN ({phases})),
                evaluation_number INTEGER NOT NULL CHECK (evaluation_number >= 1),
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                score REAL NOT NULL CHECK (score >= 0 AND score <= 100),
                blocking_failures_json TEXT NOT NULL,
                blocking_needs_human_json TEXT NOT NULL,
                eligible_for_auto_progression INTEGER NOT NULL CHECK (
                    eligible_for_auto_progression IN (0, 1)
                ),
                UNIQUE (task_id, phase, evaluation_number)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS checklist_evaluation_items (
                evaluation_id TEXT NOT NULL REFERENCES checklist_evaluations(evaluation_id),
                position INTEGER NOT NULL CHECK (position >= 0),
                key TEXT NOT NULL,
                label TEXT NOT NULL,
                weight INTEGER NOT NULL CHECK (weight >= 0),
                blocking INTEGER NOT NULL CHECK (blocking IN (0, 1)),
                status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL', 'NEEDS_HUMAN')),
                evidence TEXT NOT NULL,
                PRIMARY KEY (evaluation_id, position),
                UNIQUE (evaluation_id, key)
            )
            """
        )
        for table in ("workflow_artifacts", "checklist_evaluations", "checklist_evaluation_items"):
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_append_only_update
                BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END
                """
            )
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_append_only_delete
                BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END
                """
            )

    @staticmethod
    def _migrate_workflow_artifact_provenance(connection: sqlite3.Connection) -> None:
        """Phase 3: additive artifact-provenance columns, backfilled as HUMAN.

        Every artifact created before this migration was created only through the
        authenticated HTTP API, so backfilling ``created_by_type = 'HUMAN'`` is
        accurate, idempotent, and never rewrites existing history's meaning.
        """

        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(workflow_artifacts)")
        }
        migrations = {
            "created_by_type": (
                "ALTER TABLE workflow_artifacts ADD COLUMN created_by_type TEXT "
                "NOT NULL DEFAULT 'HUMAN'"
            ),
            "execution_id": "ALTER TABLE workflow_artifacts ADD COLUMN execution_id TEXT",
            "logical_model": "ALTER TABLE workflow_artifacts ADD COLUMN logical_model TEXT",
            "concrete_model": "ALTER TABLE workflow_artifacts ADD COLUMN concrete_model TEXT",
            "provider": "ALTER TABLE workflow_artifacts ADD COLUMN provider TEXT",
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)

    @staticmethod
    def _create_repository_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS repositories (
                repository_id TEXT PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source TEXT NOT NULL,
                default_branch TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _create_or_migrate_message_queue(connection: sqlite3.Connection) -> None:
        """Create message_queue, or rebuild a Phase 2 table to scope keys per author.

        Phase 2 made (task_id, client_message_id) unique; Phase 4 scopes it to
        (task_id, actor_id, client_message_id). SQLite cannot drop a table
        constraint, so a Phase 2 table is copied row for row into the new shape in
        this same transaction. Every Phase 2 row already stores its author in
        ``actor_id``, so nothing needs backfilling. Runs once: afterwards the
        table's SQL already contains the new constraint.
        """

        columns = """
            message_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            client_message_id TEXT NOT NULL,
            event_sequence_id INTEGER NOT NULL UNIQUE,
            actor_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            status TEXT NOT NULL,
            execution_id TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (task_id, actor_id, client_message_id),
            FOREIGN KEY (task_id) REFERENCES tasks(task_id),
            FOREIGN KEY (event_sequence_id) REFERENCES events(sequence_id)
        """
        existing = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'message_queue'"
        ).fetchone()
        if existing is None:
            connection.execute(f"CREATE TABLE message_queue ({columns})")
            return
        if "UNIQUE (task_id, actor_id, client_message_id)" in existing[0]:
            return
        names = (
            "message_id, task_id, client_message_id, event_sequence_id, actor_id, "
            "display_name, status, execution_id, error, created_at, updated_at"
        )
        connection.execute("DROP TABLE IF EXISTS message_queue_phase4_migration")
        connection.execute(f"CREATE TABLE message_queue_phase4_migration ({columns})")
        connection.execute(
            f"INSERT INTO message_queue_phase4_migration ({names}) "
            f"SELECT {names} FROM message_queue"
        )
        connection.execute("DROP TABLE message_queue")
        connection.execute("ALTER TABLE message_queue_phase4_migration RENAME TO message_queue")

    @staticmethod
    def _create_auth_tables(connection: sqlite3.Connection) -> None:
        """Demo 2 Phase 4: web users, hashed access tokens and sessions, presence."""

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_tokens (
                token_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id),
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                revoked_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS web_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id),
                token_id TEXT NOT NULL REFERENCES auth_tokens(token_id),
                session_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS task_presence (
                task_id TEXT NOT NULL,
                user_id TEXT NOT NULL REFERENCES users(user_id),
                last_seen TEXT NOT NULL,
                PRIMARY KEY (task_id, user_id)
            )
            """
        )

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
