"""Reusable application service for one-task/one-TaskSession collaboration."""

import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from uuid import uuid4

from ai_platform.approval import approve_task
from ai_platform.config import Settings
from ai_platform.events import ActorType, Event, EventType
from ai_platform.executors import AgentExecutor, AgentExecutorError
from ai_platform.graph import TaskGraphState, run_task_graph
from ai_platform.identity import HumanIdentity
from ai_platform.locks import ExecutionLockManager, LockAcquisition
from ai_platform.models import ExecutionKind, TaskDefinition, TaskRecord, TaskStatus
from ai_platform.router import ModelRouter
from ai_platform.storage import SQLiteStorage
from ai_platform.task_loader import get_task
from ai_platform.workspace import FileChange, LocalWorkspaceProvider

_CONTEXT_EVENT_LIMIT = 12


class TaskSessionError(RuntimeError):
    """A safe collaboration or lifecycle error suitable for a client."""


@dataclass(frozen=True, slots=True)
class TaskSession:
    """The durable task definition, runtime state, workspace, and shared history."""

    definition: TaskDefinition
    record: TaskRecord
    events: list[Event]
    changed_files: list[FileChange]

    @property
    def participants(self) -> list[tuple[ActorType, str, str]]:
        """Return actors seen in history, not an online-presence claim."""

        participants: dict[tuple[ActorType, str], str] = {}
        for event in self.events:
            if event.actor_type is ActorType.SYSTEM:
                continue
            key = (event.actor_type, event.actor_id)
            participants.setdefault(key, str(event.metadata.get("display_name", event.actor_id)))
        return [(*key, display_name) for key, display_name in participants.items()]

    @property
    def conversation(self) -> list[Event]:
        """Return durable human and agent conversation events."""

        return [
            event
            for event in self.events
            if event.event_type in {EventType.HUMAN_MESSAGE, EventType.AGENT_MESSAGE}
        ]


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """Observable result of recording an instruction and possibly running an agent."""

    agent_started: bool
    queued: bool = False
    state: TaskGraphState | None = None
    detail: str = ""


class TaskSessionService:
    """Application operations shared by the CLI and a future API/UI."""

    def __init__(
        self,
        settings: Settings,
        definitions: list[TaskDefinition],
        storage: SQLiteStorage,
        workspaces: LocalWorkspaceProvider,
        router: ModelRouter,
        executor_factory: Callable[[], AgentExecutor],
        lock_manager: ExecutionLockManager | None = None,
    ) -> None:
        self.settings = settings
        self.definitions = definitions
        self.storage = storage
        self.workspaces = workspaces
        self.router = router
        self.executor_factory = executor_factory
        self.locks = lock_manager or ExecutionLockManager(
            storage,
            heartbeat_seconds=settings.lock_heartbeat_seconds,
            stale_seconds=settings.lock_stale_seconds,
        )

    def get_session(self, task_id: str) -> TaskSession:
        """Load the one shared session represented by durable state plus workspace."""

        definition = self._definition(task_id)
        record = self._record(definition.id)
        changed_files = (
            self.workspaces.get_changed_files(definition.id)
            if self.workspaces.exists(definition.id)
            else []
        )
        return TaskSession(
            definition=definition,
            record=record,
            events=self.storage.get_events(definition.id),
            changed_files=changed_files,
        )

    def connect(self, task_id: str, human: HumanIdentity) -> Event:
        """Record that a human attached; this does not imply live presence."""

        definition = self._definition(task_id)
        return self._append_human_event(definition.id, EventType.HUMAN_CONNECTED, human)

    def start(self, task_id: str, human: HumanIdentity) -> TurnOutcome:
        """Start the initial agent turn in a newly copied workspace."""

        task = self._definition(task_id)
        record = self._record(task.id)
        if self.workspaces.exists(task.id):
            raise TaskSessionError(
                f"{task.id} already has a workspace. Attach to it or reset it explicitly."
            )
        if record.status is not TaskStatus.READY:
            raise TaskSessionError(
                f"{task.id} is {record.status.value.upper()}; reset it before a new start."
            )
        executor = self._preflight_executor()
        execution_id = str(uuid4())
        lock = self.locks.acquire(
            task.id,
            ExecutionKind.AGENT,
            "claude",
            execution_id,
            allowed_statuses={TaskStatus.READY},
        )
        if not lock.acquired:
            raise self._lock_error(task.id, lock)
        state = self._run_agent_turn(
            task,
            human,
            lock.owner_token,
            execution_id,
            executor,
            continuation=False,
        )
        return TurnOutcome(agent_started=True, state=state)

    def message(self, task_id: str, message: str, human: HumanIdentity) -> TurnOutcome:
        """Append a shared instruction immediately and continue a review-state task."""

        task = self._definition(task_id)
        content = self._validate_message(message)
        self._append_human_event(
            task.id,
            EventType.HUMAN_MESSAGE,
            human,
            {"message": content},
        )
        record = self._record(task.id)
        if record.status is TaskStatus.PAUSED_BY_HUMAN:
            return TurnOutcome(
                agent_started=False,
                detail="Message recorded; the task remains paused until resume.",
            )
        if record.status is not TaskStatus.WAITING_FOR_HUMAN:
            queued = record.agent_running
            return TurnOutcome(
                agent_started=False,
                queued=queued,
                detail=(
                    "Message recorded; Claude is currently working and will receive it in "
                    "the next turn."
                    if queued
                    else f"Message recorded; {task.id} is {record.status.value.upper()}."
                ),
            )
        return self._try_continuation(task, human, {TaskStatus.WAITING_FOR_HUMAN})

    def pause(self, task_id: str, human: HumanIdentity) -> bool:
        """Pause immediately or request cooperative cancellation of a live agent turn."""

        task = self._definition(task_id)
        record = self._record(task.id)
        if record.status is TaskStatus.COMPLETED:
            raise TaskSessionError(f"{task.id} is already completed")
        if record.status is TaskStatus.READY or not self.workspaces.exists(task.id):
            raise TaskSessionError(f"{task.id} has no active workspace to pause")
        if record.status is TaskStatus.PAUSED_BY_HUMAN and not record.agent_running:
            raise TaskSessionError(f"{task.id} is already paused")
        result = self.storage.request_pause(task.id)
        self._append_human_event(
            task.id,
            EventType.HUMAN_PAUSED,
            human,
            {
                "deferred": result.deferred,
                "execution_id": record.execution_id,
                "active_execution": result.active_execution.value
                if result.active_execution
                else None,
                "from": result.previous_status.value,
                "to": result.current_status.value,
            },
        )
        if result.previous_status is not result.current_status:
            self._append_status_change(task.id, result.previous_status, result.current_status)
        return result.deferred

    def shell(
        self,
        task_id: str,
        human: HumanIdentity,
        runner: Callable[[Path], int] | None = None,
    ) -> list[FileChange]:
        """Open one exclusive human shell and attribute Git-observed changes."""

        task = self._definition(task_id)
        record = self._record(task.id)
        if record.agent_running:
            raise TaskSessionError(
                f"{task.id} is currently being modified by Claude. Pause it before takeover."
            )
        if record.status is not TaskStatus.PAUSED_BY_HUMAN:
            raise TaskSessionError(
                f"{task.id} must be PAUSED_BY_HUMAN before opening a shell"
            )
        execution_id = str(uuid4())
        lock = self.locks.acquire(
            task.id,
            ExecutionKind.HUMAN_SHELL,
            human.actor_id,
            execution_id,
            allowed_statuses={TaskStatus.PAUSED_BY_HUMAN},
        )
        if not lock.acquired:
            latest = self._record(task.id)
            if latest.agent_running:
                raise TaskSessionError(
                    f"{task.id} is currently being modified by Claude. Pause it before takeover."
                )
            raise self._lock_error(task.id, lock)

        try:
            workspace = self.workspaces.get_path(task.id)
            before = self.workspaces.snapshot(task.id)
            self._append_human_event(
                task.id,
                EventType.HUMAN_SHELL_OPENED,
                human,
                {
                    "workspace": str(workspace),
                    "execution_id": execution_id,
                    "changed_files_before": sorted(before.files),
                    "git_state_before": before.fingerprint,
                },
            )
            exit_code: int | None = None
            changes: list[FileChange] = []
            try:
                with self.locks.heartbeat(task.id, lock.owner_token):
                    exit_code = (runner or self._run_interactive_shell)(workspace)
            finally:
                after = self.workspaces.snapshot(task.id)
                changes = self.workspaces.changes_between(before, after)
                if changes:
                    self._append_human_event(
                        task.id,
                        EventType.HUMAN_WORKSPACE_CHANGED,
                        human,
                        {
                            "execution_id": execution_id,
                            "files": [change.model_dump() for change in changes],
                        },
                    )
                self._append_human_event(
                    task.id,
                    EventType.HUMAN_SHELL_CLOSED,
                    human,
                    {
                        "execution_id": execution_id,
                        "exit_code": exit_code,
                        "git_state_after": after.fingerprint,
                    },
                )
            return changes
        finally:
            self.storage.release_execution(task.id, lock.owner_token)

    def resume(
        self,
        task_id: str,
        human: HumanIdentity,
        message: str | None = None,
    ) -> TurnOutcome:
        """Continue from the current human-edited workspace after a pause."""

        task = self._definition(task_id)
        record = self._record(task.id)
        if record.status is not TaskStatus.PAUSED_BY_HUMAN:
            raise TaskSessionError(f"{task.id} is not PAUSED_BY_HUMAN")
        if message is not None:
            content = self._validate_message(message)
            self._append_human_event(
                task.id,
                EventType.HUMAN_MESSAGE,
                human,
                {"message": content},
            )
        executor = self._preflight_executor()
        execution_id = str(uuid4())
        lock = self.locks.acquire(
            task.id,
            ExecutionKind.AGENT,
            "claude",
            execution_id,
            allowed_statuses={TaskStatus.PAUSED_BY_HUMAN},
        )
        if not lock.acquired:
            raise self._lock_error(task.id, lock)
        self._append_human_event(task.id, EventType.HUMAN_RESUMED, human)
        state = self._run_agent_turn(
            task,
            human,
            lock.owner_token,
            execution_id,
            executor,
            continuation=True,
        )
        return TurnOutcome(agent_started=True, state=state)

    def reject(self, task_id: str, message: str, human: HumanIdentity) -> TurnOutcome:
        """Record review rejection and continue against the same workspace."""

        task = self._definition(task_id)
        content = self._validate_message(message)
        record = self._record(task.id)
        if record.status is not TaskStatus.WAITING_FOR_HUMAN:
            raise TaskSessionError(
                f"{task.id} must be WAITING_FOR_HUMAN before review rejection"
            )
        self._append_human_event(
            task.id,
            EventType.HUMAN_REJECTED,
            human,
            {"message": content},
        )
        self._append_human_event(
            task.id,
            EventType.HUMAN_MESSAGE,
            human,
            {"message": content},
        )
        return self._try_continuation(task, human, {TaskStatus.WAITING_FOR_HUMAN})

    def approve(self, task_id: str, human: HumanIdentity) -> None:
        """Attribute and persist a human approval decision."""

        task = self._definition(task_id)
        approve_task(self.storage, task.id, human.actor_id, human.display_name)

    def reset(self, task_id: str, human: HumanIdentity) -> Path:
        """Reset runtime/workspace while retaining the complete append-only history."""

        task = self._definition(task_id)
        record = self._record(task.id)
        if record.active_execution is not None:
            raise TaskSessionError(f"{task.id} currently has an active workspace writer")
        workspace = self.workspaces.get_path(task.id)
        workspace_existed = self.workspaces.exists(task.id)
        self.workspaces.destroy(task.id)
        self.storage.reset_task_runtime(task.id)
        if workspace_existed:
            self._append_human_event(
                task.id,
                EventType.WORKSPACE_RESET,
                human,
                {"deleted_path": str(workspace)},
            )
        self._append_human_event(
            task.id,
            EventType.TASK_RESET,
            human,
            {"previous_status": record.status.value},
        )
        return workspace

    def follow_events(
        self,
        task_id: str,
        after_sequence_id: int,
        *,
        poll_interval_seconds: float = 0.35,
    ) -> Iterator[Event]:
        """Poll SQLite and yield each new event once until the caller closes the iterator."""

        task = self._definition(task_id)
        cursor = after_sequence_id
        while True:
            events = self.storage.get_events_after(task.id, cursor)
            if not events:
                sleep(poll_interval_seconds)
                continue
            for event in events:
                if event.sequence_id is None:
                    continue
                cursor = event.sequence_id
                yield event

    def _try_continuation(
        self,
        task: TaskDefinition,
        human: HumanIdentity,
        allowed_statuses: set[TaskStatus],
    ) -> TurnOutcome:
        executor = self._preflight_executor()
        execution_id = str(uuid4())
        lock = self.locks.acquire(
            task.id,
            ExecutionKind.AGENT,
            "claude",
            execution_id,
            allowed_statuses=allowed_statuses,
        )
        if not lock.acquired:
            latest = self._record(task.id)
            return TurnOutcome(
                agent_started=False,
                queued=latest.agent_running,
                detail=(
                    "Message recorded; Claude is currently working. The instruction will be "
                    "available for the next turn."
                    if latest.agent_running
                    else f"Message recorded; {task.id} is now {latest.status.value.upper()}."
                ),
            )
        state = self._run_agent_turn(
            task,
            human,
            lock.owner_token,
            execution_id,
            executor,
            continuation=True,
        )
        return TurnOutcome(agent_started=True, state=state)

    def _run_agent_turn(
        self,
        task: TaskDefinition,
        human: HumanIdentity,
        owner: str,
        execution_id: str,
        executor: AgentExecutor,
        *,
        continuation: bool,
    ) -> TaskGraphState:
        human_messages, agent_messages = self._current_conversation_context(task.id)
        human_workspace_changed = self._human_changed_workspace_since_last_agent(task.id)
        try:
            with self.locks.heartbeat(task.id, owner):
                return run_task_graph(
                    task,
                    self.router,
                    self.storage,
                    self.workspaces,
                    executor,
                    self.settings.checkpoint_db_path,
                    verification_timeout_seconds=self.settings.verification_timeout_seconds,
                    max_attempts_per_tier=self.settings.max_attempts_per_tier,
                    execution_id=execution_id,
                    actor_id=human.actor_id,
                    continuation=continuation,
                    human_messages=human_messages,
                    recent_agent_messages=agent_messages,
                    human_workspace_changed=human_workspace_changed,
                )
        except KeyboardInterrupt:
            previous = self._record(task.id).status
            self.storage.update_task_status(task.id, TaskStatus.PAUSED_BY_HUMAN)
            if previous is not TaskStatus.PAUSED_BY_HUMAN:
                self._append_status_change(task.id, previous, TaskStatus.PAUSED_BY_HUMAN)
            raise
        except Exception as error:
            if not self.storage.is_pause_requested(task.id):
                previous = self._record(task.id).status
                self.storage.update_task_status(task.id, TaskStatus.FAILED)
                if previous is not TaskStatus.FAILED:
                    self._append_status_change(task.id, previous, TaskStatus.FAILED)
                self.storage.append_event(
                    Event(
                        task_id=task.id,
                        event_type=EventType.TASK_FAILED,
                        actor_type=ActorType.SYSTEM,
                        actor_id="task-session-service",
                        metadata={
                            "execution_id": execution_id,
                            "error": (str(error) or type(error).__name__)[:2000],
                        },
                    )
                )
            raise
        finally:
            pause_transition = self.storage.release_execution(task.id, owner)
            if pause_transition:
                self._append_status_change(task.id, *pause_transition)

    def _current_conversation_context(self, task_id: str) -> tuple[list[str], list[str]]:
        events = self.storage.get_recent_events(
            task_id,
            {
                EventType.HUMAN_MESSAGE,
                EventType.AGENT_MESSAGE,
                EventType.AGENT_COMPLETED,
            },
            limit=_CONTEXT_EVENT_LIMIT,
        )
        humans: list[str] = []
        agents: list[str] = []
        for event in events:
            if event.event_type is EventType.HUMAN_MESSAGE:
                humans.append(f"{event.actor_id}: {event.metadata.get('message', '')}")
            elif event.event_type is EventType.AGENT_MESSAGE:
                agents.append(str(event.metadata.get("message", "")))
            elif summary := event.metadata.get("summary"):
                agents.append(str(summary))
        return humans[-8:], agents[-4:]

    def _human_changed_workspace_since_last_agent(self, task_id: str) -> bool:
        events = self.storage.get_events(task_id)
        last_agent = max(
            (
                event.sequence_id or 0
                for event in events
                if event.event_type is EventType.AGENT_STARTED
            ),
            default=0,
        )
        return any(
            event.event_type is EventType.HUMAN_WORKSPACE_CHANGED
            and (event.sequence_id or 0) > last_agent
            for event in events
        )

    def _definition(self, task_id: str) -> TaskDefinition:
        try:
            return get_task(self.definitions, task_id)
        except KeyError as error:
            raise TaskSessionError(f"Unknown task ID: {task_id}") from error

    def _record(self, task_id: str) -> TaskRecord:
        record = self.storage.get_task(task_id)
        if record is None:
            raise TaskSessionError(f"No runtime state exists for {task_id}")
        return record

    def recover_stale_locks(self, *, recovered_by: str) -> list[str]:
        """Recover demonstrably stale locks and pause their unknown workspace state."""

        return self.locks.recover_all_stale_locks(recovered_by=recovered_by)

    def _append_human_event(
        self,
        task_id: str,
        event_type: EventType,
        human: HumanIdentity,
        metadata: dict | None = None,
    ) -> Event:
        details = {"display_name": human.display_name, **(metadata or {})}
        return self.storage.append_event(
            Event(
                task_id=task_id,
                event_type=event_type,
                actor_type=ActorType.HUMAN,
                actor_id=human.actor_id,
                metadata=details,
            )
        )

    def _append_status_change(
        self,
        task_id: str,
        previous: TaskStatus,
        current: TaskStatus,
    ) -> None:
        self.storage.append_event(
            Event(
                task_id=task_id,
                event_type=EventType.STATUS_CHANGED,
                actor_type=ActorType.SYSTEM,
                actor_id="task-session-service",
                metadata={"from": previous.value, "to": current.value},
            )
        )

    def _preflight_executor(self) -> AgentExecutor:
        executor = self.executor_factory()
        try:
            executor.preflight()
        except AgentExecutorError as error:
            raise TaskSessionError(str(error)) from error
        except Exception as error:
            message = str(error) or type(error).__name__
            raise TaskSessionError(f"Agent executor preflight failed: {message[:1000]}") from error
        return executor

    def _lock_error(self, task_id: str, acquisition: LockAcquisition) -> TaskSessionError:
        if acquisition.recovered_stale_lock:
            return TaskSessionError(
                f"Recovered a stale lock for {task_id} and paused the task. "
                "Inspect its diff and trace, then resume explicitly."
            )
        record = acquisition.current
        if record is None or record.active_execution is None:
            return TaskSessionError(f"{task_id} could not acquire its workspace lock")
        inspection = self.locks.inspect(record)
        age = (
            f"{inspection.heartbeat_age_seconds:.1f}s ago"
            if inspection.heartbeat_age_seconds is not None
            else "unknown"
        )
        actor = record.execution_actor_id or record.execution_owner or "unknown"
        return TaskSessionError(
            f"{task_id} has a {inspection.health.value} {record.active_execution.value} lock "
            f"owned by {actor} on {record.execution_hostname or 'unknown host'} "
            f"(pid {record.execution_pid or 'unknown'}, heartbeat {age})."
        )

    @staticmethod
    def _validate_message(message: str) -> str:
        content = message.strip()
        if not content:
            raise TaskSessionError("Message must not be empty")
        if len(content) > 8000:
            raise TaskSessionError("Message must be 8000 characters or fewer")
        return content

    @staticmethod
    def _run_interactive_shell(workspace: Path) -> int:
        if os.name == "nt":
            configured = os.getenv("COMSPEC")
            shell_path = configured if configured and Path(configured).is_file() else None
            shell_path = shell_path or shutil.which("cmd.exe")
        else:
            configured = os.getenv("SHELL")
            shell_path = configured if configured and Path(configured).is_file() else None
            shell_path = shell_path or shutil.which("sh")
        if not shell_path:
            raise TaskSessionError("No interactive operating-system shell was found")
        completed = subprocess.run([shell_path], cwd=workspace, check=False, shell=False)
        return completed.returncode
