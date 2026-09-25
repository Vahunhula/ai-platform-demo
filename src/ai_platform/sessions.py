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
from ai_platform.models import (
    ExecutionKind,
    TaskDefinition,
    TaskRecord,
    TaskStatus,
)
from ai_platform.repositories import RepositoryService
from ai_platform.router import ModelRouter
from ai_platform.storage import SQLiteStorage
from ai_platform.task_loader import get_task
from ai_platform.verification import BaselineContext
from ai_platform.workflow import TransitionMode, WorkflowPhase
from ai_platform.workspace import FileChange, LocalWorkspaceProvider

_CONTEXT_EVENT_LIMIT = 12
MAX_MESSAGE_LENGTH = 8000


class TaskSessionError(RuntimeError):
    """A safe collaboration or lifecycle error suitable for a client."""


class TaskNotFoundError(TaskSessionError):
    """The requested task ID has no definition or no persisted runtime state."""


class TaskLockedError(TaskSessionError):
    """Another live execution owns the task's workspace writer lock."""


class ExecutorUnavailableError(TaskSessionError):
    """The configured agent executor failed its preflight check."""


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
    # True when no agent turn ran but the outcome is final for this instruction
    # (e.g. it arrived while the task rests in HUMAN_REVIEW): a queued browser
    # message should complete, not be endlessly retried like a genuine race.
    terminal: bool = False


@dataclass(frozen=True, slots=True)
class PreparedTurn:
    """An agent turn whose preconditions passed and whose writer lock is already held.

    ``prepare_*`` methods do the quick part of a lifecycle command (checks, intent
    events, executor preflight, lock acquisition) exactly as the CLI always has;
    ``run_prepared`` runs the long part. The CLI runs both back to back; the HTTP
    API returns after preparing and runs the turn in the background runner.
    """

    task: TaskDefinition
    human: HumanIdentity
    owner: str
    execution_id: str
    executor: AgentExecutor
    continuation: bool
    through_sequence_id: int | None = None


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
        repositories: RepositoryService | None = None,
        lock_manager: ExecutionLockManager | None = None,
    ) -> None:
        self.settings = settings
        self.definitions = definitions
        self.storage = storage
        self.workspaces = workspaces
        self.router = router
        self.executor_factory = executor_factory
        self.repositories = repositories
        self.locks = lock_manager or ExecutionLockManager(
            storage,
            heartbeat_seconds=settings.lock_heartbeat_seconds,
            stale_seconds=settings.lock_stale_seconds,
        )

    def get_session(self, task_id: str) -> TaskSession:
        """Load the one shared session represented by durable state plus workspace."""

        definition = self._definition(task_id)
        record = self._record(definition.id)
        workspace = self.resolve_workspace(definition.id, record=record)
        changed_files = (
            self.workspaces.get_changed_files(definition.id) if workspace.is_dir() else []
        )
        return TaskSession(
            definition=definition,
            record=record,
            events=self.storage.get_events(definition.id),
            changed_files=changed_files,
        )

    def list_tasks(self) -> list[TaskRecord]:
        """Return current persisted task records in stable task-ID order."""

        return self.storage.list_tasks()

    def get_definition(self, task_id: str) -> TaskDefinition:
        """Return a validated task definition through the application service."""

        definition = self._definition(task_id)
        self._record(definition.id)
        return definition

    def get_events(self, task_id: str) -> list[Event]:
        """Return one task's durable event stream in sequence order."""

        task = self._definition(task_id)
        self._record(task.id)
        return self.storage.get_events(task.id)

    def get_diff(self, task_id: str) -> str:
        """Return the task workspace diff, or an empty diff before workspace creation."""

        task = self._definition(task_id)
        workspace = self.resolve_workspace(task.id)
        if not workspace.is_dir():
            return ""
        return self.workspaces.get_diff(task.id)

    def resolve_workspace(
        self,
        task_id: str,
        *,
        record: TaskRecord | None = None,
        require_exists: bool = False,
    ) -> Path:
        """Resolve the one authoritative workspace assigned to a task.

        Legacy task rows predate managed repositories and may contain historical
        paths from another deployment, so their identity remains the configured
        workspace root plus task ID. Browser-created tasks are eagerly provisioned;
        their persisted path must exist as an identity and must match that canonical
        task path. A mismatch is an error, never a fallback to either location.
        """

        task = self._definition(task_id)
        current = record or self._record(task.id)
        canonical = self.workspaces.get_path(task.id).resolve()
        if current.repository_id is not None:
            if not current.workspace_path:
                raise TaskSessionError(f"{task.id} has no persisted workspace identity")
            persisted = Path(current.workspace_path).resolve()
            if persisted != canonical:
                raise TaskSessionError(f"{task.id} has an invalid workspace identity")
            workspace = persisted
        else:
            workspace = canonical
        if require_exists and not workspace.is_dir():
            raise TaskSessionError(f"{task.id} is missing its provisioned workspace")
        return workspace

    def workspace_exists(self, task_id: str) -> bool:
        """Return whether the task's authoritative workspace currently exists."""

        return self.resolve_workspace(task_id).is_dir()

    def connect(self, task_id: str, human: HumanIdentity) -> Event:
        """Record that a human attached; this does not imply live presence."""

        definition = self._definition(task_id)
        return self._append_human_event(definition.id, EventType.HUMAN_CONNECTED, human)

    def start(self, task_id: str, human: HumanIdentity) -> TurnOutcome:
        """Start the initial agent turn in a newly copied workspace."""

        return self.run_prepared(self.prepare_start(task_id, human))

    def prepare_start(
        self,
        task_id: str,
        human: HumanIdentity,
        *,
        execution_id: str | None = None,
    ) -> PreparedTurn:
        """Check a READY task, preflight the executor and take its writer lock."""

        task = self._definition(task_id)
        record = self._record(task.id)
        workspace = self.resolve_workspace(task.id, record=record)
        if workspace.is_dir() and record.repository_id is None:
            raise TaskSessionError(
                f"{task.id} already has a workspace. Attach to it or reset it explicitly."
            )
        if record.repository_id is not None and not workspace.is_dir():
            raise TaskSessionError(f"{task.id} is missing its provisioned workspace")
        if record.status is not TaskStatus.READY:
            raise TaskSessionError(
                f"{task.id} is {record.status.value.upper()}; reset it before a new start."
            )
        self._check_starting_phase_available(task.id, record.workflow_phase)
        executor = self._preflight_executor()
        execution_id = execution_id or str(uuid4())
        lock = self.locks.acquire(
            task.id,
            ExecutionKind.AGENT,
            "claude",
            execution_id,
            allowed_statuses={TaskStatus.READY},
        )
        if not lock.acquired:
            raise self._lock_error(task.id, lock)
        return PreparedTurn(task, human, lock.owner_token, execution_id, executor, False)

    def run_prepared(self, prepared: PreparedTurn) -> TurnOutcome:
        """Run a prepared turn to its end; the writer lock is always released."""

        state = self._run_agent_turn(
            prepared.task,
            prepared.human,
            prepared.owner,
            prepared.execution_id,
            prepared.executor,
            continuation=prepared.continuation,
            through_sequence_id=prepared.through_sequence_id,
        )
        return TurnOutcome(agent_started=True, state=state)

    def message(self, task_id: str, message: str, human: HumanIdentity) -> TurnOutcome:
        """Append a shared instruction immediately and continue a review-state task."""

        task = self._definition(task_id)
        content = self.validate_message(message)
        self._append_human_event(
            task.id,
            EventType.HUMAN_MESSAGE,
            human,
            {"message": content},
        )
        return self.continue_conversation(task.id, human)

    def continue_conversation(
        self,
        task_id: str,
        human: HumanIdentity,
        *,
        execution_id: str | None = None,
        through_sequence_id: int | None = None,
    ) -> TurnOutcome:
        """Run the next agent turn for already-recorded human messages when state permits.

        This is the single message-continuation path shared by the CLI (``message``)
        and the browser message runner. ``through_sequence_id`` limits the human
        instructions in the turn context to messages recorded up to that event, so a
        queued browser message is answered in its own FIFO turn.
        """

        task = self._definition(task_id)
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
        if record.workflow_phase is WorkflowPhase.HUMAN_REVIEW:
            # HUMAN_REVIEW runs no agent automatically: a plain chat message is
            # recorded (by the caller) but does not start a turn. Approve or
            # Reject are the only ways forward from here. This is final, not a
            # race to retry: a queued browser message completes as delivered.
            return TurnOutcome(
                agent_started=False,
                terminal=True,
                detail=(
                    f"Message recorded; {task.id} is in Human Review. Use Approve or Reject "
                    "to continue."
                ),
            )
        return self._try_continuation(
            task,
            human,
            {TaskStatus.WAITING_FOR_HUMAN},
            execution_id=execution_id,
            through_sequence_id=through_sequence_id,
        )

    def human_message_event(
        self,
        task_id: str,
        content: str,
        human: HumanIdentity,
        metadata: dict | None = None,
    ) -> Event:
        """Build (without persisting) the canonical HUMAN_MESSAGE event for a task."""

        task = self._definition(task_id)
        return self._human_event(
            task.id,
            EventType.HUMAN_MESSAGE,
            human,
            {"message": self.validate_message(content), **(metadata or {})},
        )

    def pause(self, task_id: str, human: HumanIdentity) -> bool:
        """Pause immediately or request cooperative cancellation of a live agent turn."""

        task = self._definition(task_id)
        record = self._record(task.id)
        workspace = self.resolve_workspace(task.id, record=record)
        if record.status is TaskStatus.COMPLETED:
            raise TaskSessionError(f"{task.id} is already completed")
        if record.status is TaskStatus.READY or not workspace.is_dir():
            raise TaskSessionError(f"{task.id} has no active workspace to pause")
        if record.status is TaskStatus.PAUSED_BY_HUMAN and not record.agent_running:
            raise TaskSessionError(f"{task.id} is already paused")
        if record.pause_requested:
            raise TaskSessionError(f"{task.id} already has a pause request")
        result = self.storage.request_pause(task.id)
        if result is None:  # a concurrent pause won the atomic update
            raise TaskSessionError(f"{task.id} is already paused")
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
            raise TaskSessionError(f"{task.id} must be PAUSED_BY_HUMAN before opening a shell")
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
            workspace = self.resolve_workspace(task.id, require_exists=True)
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

        return self.run_prepared(self.prepare_resume(task_id, human, message))

    def prepare_resume(
        self,
        task_id: str,
        human: HumanIdentity,
        message: str | None = None,
        *,
        execution_id: str | None = None,
    ) -> PreparedTurn:
        """Take the writer lock, then record the optional instruction and the resume.

        The lock is the cross-process serialization point: only the request that
        wins it records intent events, so concurrent resumes never duplicate them.
        """

        task = self._definition(task_id)
        record = self._record(task.id)
        self.resolve_workspace(task.id, record=record, require_exists=True)
        if record.status is not TaskStatus.PAUSED_BY_HUMAN:
            raise TaskSessionError(f"{task.id} is not PAUSED_BY_HUMAN")
        content = self._validate_message(message) if message is not None else None
        self._check_starting_phase_available(task.id, record.workflow_phase)
        executor = self._preflight_executor()
        execution_id = execution_id or str(uuid4())
        lock = self.locks.acquire(
            task.id,
            ExecutionKind.AGENT,
            "claude",
            execution_id,
            allowed_statuses={TaskStatus.PAUSED_BY_HUMAN},
        )
        if not lock.acquired:
            raise self._lock_error(task.id, lock)
        if content is not None:
            self._append_human_event(
                task.id,
                EventType.HUMAN_MESSAGE,
                human,
                {"message": content},
            )
        self._append_human_event(task.id, EventType.HUMAN_RESUMED, human)
        return PreparedTurn(task, human, lock.owner_token, execution_id, executor, True)

    def reject(self, task_id: str, message: str, human: HumanIdentity) -> TurnOutcome:
        """Record review rejection and continue against the same workspace."""

        return self.run_prepared(self.prepare_reject(task_id, message, human))

    def prepare_reject(
        self,
        task_id: str,
        message: str,
        human: HumanIdentity,
        *,
        execution_id: str | None = None,
    ) -> PreparedTurn:
        """Take the lock for the correction turn, then record the rejection feedback.

        Only the request that wins the writer lock records HUMAN_REJECTED and the
        feedback message, so concurrent rejections never duplicate them.
        """

        task = self._definition(task_id)
        content = self._validate_message(message)
        record = self._record(task.id)
        if record.status is not TaskStatus.WAITING_FOR_HUMAN:
            raise TaskSessionError(f"{task.id} must be WAITING_FOR_HUMAN before review rejection")
        if record.workflow_phase is WorkflowPhase.HUMAN_REVIEW:
            # The default correction target: a human rejecting the final review
            # sends the task back to Implementation with their feedback. This is
            # an explicit human decision, so it is recorded as MANUAL, never as
            # an automatic gate transition.
            self._transition_phase_manual(
                task.id,
                WorkflowPhase.HUMAN_REVIEW,
                WorkflowPhase.IMPLEMENTATION,
                human,
                reason="Rejected from Human Review",
            )
        prepared = self._prepare_continuation(
            task, human, {TaskStatus.WAITING_FOR_HUMAN}, execution_id=execution_id
        )
        if isinstance(prepared, TurnOutcome):
            raise TaskLockedError(f"{task.id} is being changed by another execution")
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
        return prepared

    def approve(self, task_id: str, human: HumanIdentity) -> None:
        """Attribute and persist a human approval decision."""

        task = self._definition(task_id)
        approve_task(self.storage, task.id, human.actor_id, human.display_name)

    def reset(self, task_id: str, human: HumanIdentity) -> Path:
        """Reset runtime/workspace while retaining the complete append-only history."""

        task = self._definition(task_id)
        record = self._record(task.id)
        workspace = self.resolve_workspace(task.id, record=record)
        if record.active_execution is not None:
            raise TaskSessionError(f"{task.id} currently has an active workspace writer")
        # Hold the one-writer lock while the workspace is deleted, so no concurrent
        # start/resume (from any process) can run against a disappearing workspace.
        # Demo 1's "human_shell" kind is reused for this exclusive human operation.
        lock = self.locks.acquire(
            task.id,
            ExecutionKind.HUMAN_SHELL,
            human.actor_id,
            str(uuid4()),
            allowed_statuses=set(TaskStatus),
        )
        if not lock.acquired:
            raise self._lock_error(task.id, lock)
        workspace_existed = workspace.is_dir()
        try:
            self.workspaces.destroy(task.id)
            if record.repository_id:
                if self.repositories is None:
                    raise TaskSessionError("Repository registry is unavailable")
                repository = self.repositories.get(record.repository_id)
                source_commit = self.repositories.resolve_commit(
                    Path(repository.source), record.base_branch
                )
                workspace = self.workspaces.create(
                    task.id, Path(repository.source), source_commit
                )
                self.storage.reset_managed_task_runtime(task.id, workspace, lock.owner_token)
            else:
                # Resets runtime state and releases the lock in one atomic update.
                self.storage.reset_task_runtime(task.id, owner=lock.owner_token)
        except BaseException:
            self.storage.release_execution(task.id, lock.owner_token)
            raise
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
            {
                "previous_status": record.status.value,
                **({"source_commit": source_commit} if record.repository_id else {}),
            },
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
        *,
        execution_id: str | None = None,
        through_sequence_id: int | None = None,
    ) -> TurnOutcome:
        prepared = self._prepare_continuation(
            task,
            human,
            allowed_statuses,
            execution_id=execution_id,
            through_sequence_id=through_sequence_id,
        )
        return prepared if isinstance(prepared, TurnOutcome) else self.run_prepared(prepared)

    def _prepare_continuation(
        self,
        task: TaskDefinition,
        human: HumanIdentity,
        allowed_statuses: set[TaskStatus],
        *,
        execution_id: str | None = None,
        through_sequence_id: int | None = None,
    ) -> PreparedTurn | TurnOutcome:
        self._check_starting_phase_available(task.id, self._record(task.id).workflow_phase)
        executor = self._preflight_executor()
        execution_id = execution_id or str(uuid4())
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
        return PreparedTurn(
            task,
            human,
            lock.owner_token,
            execution_id,
            executor,
            True,
            through_sequence_id,
        )

    def _run_agent_turn(
        self,
        task: TaskDefinition,
        human: HumanIdentity,
        owner: str,
        execution_id: str,
        executor: AgentExecutor,
        *,
        continuation: bool,
        through_sequence_id: int | None = None,
    ) -> TaskGraphState:
        human_messages, agent_messages = self._current_conversation_context(
            task.id, through_sequence_id
        )
        human_workspace_changed = self._human_changed_workspace_since_last_agent(task.id)
        try:
            workspace = self.resolve_workspace(task.id, require_exists=continuation)
            baseline_context = self._baseline_context(task.id)
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
                    workspace_path=workspace,
                    baseline_context=baseline_context,
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

    def _baseline_context(self, task_id: str) -> BaselineContext | None:
        """Resolve registered dynamic tasks to a clean immutable source commit."""

        record = self._record(task_id)
        if not record.repository_id or not record.base_branch or self.repositories is None:
            return None
        repository = self.repositories.get(record.repository_id)
        source = Path(repository.source)
        source_commit = next(
            (
                str(event.metadata["source_commit"])
                for event in reversed(self.storage.get_events(task_id))
                if isinstance(event.metadata.get("source_commit"), str)
                and len(str(event.metadata["source_commit"])) == 40
            ),
            None,
        )
        return BaselineContext(
            repository_id=repository.id,
            source_repository=source,
            source_commit=source_commit
            or self.repositories.resolve_commit(source, record.base_branch),
        )

    def _current_conversation_context(
        self,
        task_id: str,
        through_sequence_id: int | None = None,
    ) -> tuple[list[str], list[str]]:
        if through_sequence_id is None:
            events = self.storage.get_recent_events(
                task_id,
                {
                    EventType.HUMAN_MESSAGE,
                    EventType.AGENT_MESSAGE,
                    EventType.AGENT_COMPLETED,
                },
                limit=_CONTEXT_EVENT_LIMIT,
            )
        else:
            # Human instructions stop at the queued message; agent replies do not.
            events = sorted(
                [
                    *self.storage.get_recent_events(
                        task_id,
                        {EventType.HUMAN_MESSAGE},
                        limit=_CONTEXT_EVENT_LIMIT,
                        through_sequence_id=through_sequence_id,
                    ),
                    *self.storage.get_recent_events(
                        task_id,
                        {EventType.AGENT_MESSAGE, EventType.AGENT_COMPLETED},
                        limit=_CONTEXT_EVENT_LIMIT,
                    ),
                ],
                key=lambda event: event.sequence_id or 0,
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
        except KeyError:
            record = self.storage.get_task(task_id.upper())
            definition = record.managed_definition() if record else None
            if definition is None:
                raise TaskNotFoundError(f"Unknown task ID: {task_id}") from None
            return definition

    def _record(self, task_id: str) -> TaskRecord:
        record = self.storage.get_task(task_id)
        if record is None:
            raise TaskNotFoundError(f"No runtime state exists for {task_id}")
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
        return self.storage.append_event(self._human_event(task_id, event_type, human, metadata))

    @staticmethod
    def _human_event(
        task_id: str,
        event_type: EventType,
        human: HumanIdentity,
        metadata: dict | None = None,
    ) -> Event:
        details = {"display_name": human.display_name, **(metadata or {})}
        return Event(
            task_id=task_id,
            event_type=event_type,
            actor_type=ActorType.HUMAN,
            actor_id=human.actor_id,
            metadata=details,
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

    def _check_starting_phase_available(self, task_id: str, phase: WorkflowPhase) -> None:
        """Fail fast if the phase a turn is about to start in has no usable model.

        HUMAN_REVIEW never runs an agent, so it has nothing to check: a turn
        starting there (a defensive case the graph itself also handles) simply
        re-confirms the wait state.
        """

        if phase is WorkflowPhase.HUMAN_REVIEW:
            return
        self.router.resolve(task_id, phase, storage=self.storage)

    def _transition_phase_manual(
        self,
        task_id: str,
        expected_from: WorkflowPhase,
        target: WorkflowPhase,
        human: HumanIdentity,
        *,
        reason: str,
    ) -> None:
        event = Event(
            task_id=task_id,
            event_type=EventType.WORKFLOW_PHASE_CHANGED,
            actor_type=ActorType.HUMAN,
            actor_id=human.actor_id,
            metadata={
                "from_phase": expected_from.value,
                "to_phase": target.value,
                "transition_mode": TransitionMode.MANUAL.value,
                "display_name": human.display_name,
                "reason": reason,
            },
        )
        if not self.storage.transition_workflow_phase(task_id, expected_from, target, event):
            raise TaskSessionError(f"{task_id} is no longer in {expected_from.value}")

    def _preflight_executor(self) -> AgentExecutor:
        executor = self.executor_factory()
        try:
            executor.preflight()
        except AgentExecutorError as error:
            raise ExecutorUnavailableError(str(error)) from error
        except Exception as error:
            message = str(error) or type(error).__name__
            raise ExecutorUnavailableError(
                f"Agent executor preflight failed: {message[:1000]}"
            ) from error
        return executor

    def _lock_error(self, task_id: str, acquisition: LockAcquisition) -> TaskSessionError:
        if acquisition.recovered_stale_lock:
            return TaskSessionError(
                f"Recovered a stale lock for {task_id} and paused the task. "
                "Inspect its diff and trace, then resume explicitly."
            )
        record = acquisition.current
        if record is None or record.active_execution is None:
            return TaskLockedError(f"{task_id} could not acquire its workspace lock")
        inspection = self.locks.inspect(record)
        age = (
            f"{inspection.heartbeat_age_seconds:.1f}s ago"
            if inspection.heartbeat_age_seconds is not None
            else "unknown"
        )
        actor = record.execution_actor_id or record.execution_owner or "unknown"
        return TaskLockedError(
            f"{task_id} has a {inspection.health.value} {record.active_execution.value} lock "
            f"owned by {actor} on {record.execution_hostname or 'unknown host'} "
            f"(pid {record.execution_pid or 'unknown'}, heartbeat {age})."
        )

    @staticmethod
    def validate_message(message: str) -> str:
        """Normalize one human instruction or raise a client-safe error."""

        content = message.strip()
        if not content:
            raise TaskSessionError("Message must not be empty")
        if len(content) > MAX_MESSAGE_LENGTH:
            raise TaskSessionError(f"Message must be {MAX_MESSAGE_LENGTH} characters or fewer")
        return content

    _validate_message = validate_message

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
