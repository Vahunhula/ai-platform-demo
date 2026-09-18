"""Browser task lifecycle controls on top of the shared TaskSessionService.

Every control calls the same core method as the CLI command of the same name:

    start   → TaskSessionService.prepare_start   (+ run_prepared in the runner)
    resume  → TaskSessionService.prepare_resume  (+ run_prepared in the runner)
    reject  → TaskSessionService.prepare_reject  (+ run_prepared in the runner)
    pause   → TaskSessionService.pause
    approve → TaskSessionService.approve         (approval.approve_task)
    reset   → TaskSessionService.reset

This module adds only what an HTTP interface needs: the server-configured actor,
one place that decides which actions are currently available (the UI renders it),
idempotency keys for turn-launching actions, and client-safe error messages.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from uuid import NAMESPACE_URL, uuid5

from ai_platform.approval import ApprovalError
from ai_platform.conversation import ConversationService
from ai_platform.models import (
    ExecutionKind,
    MessageStatus,
    TaskRecord,
    TaskStatus,
    VerificationStatus,
)
from ai_platform.runner import TaskTurnRunner
from ai_platform.sessions import (
    ExecutorUnavailableError,
    PreparedTurn,
    TaskLockedError,
    TaskNotFoundError,
    TaskSessionError,
    TaskSessionService,
    TurnOutcome,
)
from ai_platform.storage import SQLiteStorage


class ControlAction(StrEnum):
    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    APPROVE = "approve"
    REJECT = "reject"
    RESET = "reset"


# Actions that launch an agent turn: asynchronous, idempotent by client_action_id.
TURN_ACTIONS = frozenset({ControlAction.START, ControlAction.RESUME, ControlAction.REJECT})
_ACTIVE_STATUSES = frozenset({TaskStatus.ANALYZING, TaskStatus.IMPLEMENTING, TaskStatus.VERIFYING})
_IDEMPOTENCY_NAMESPACE = uuid5(NAMESPACE_URL, "ai-platform/task-control")
OWNED = "Another execution currently owns this task."


class ActionConflictError(TaskSessionError):
    """The action is not valid in the task's current state."""


class RunnerUnavailableError(TaskSessionError):
    """This API process does not run agent turns (AI_PLATFORM_ENABLE_RUNNER unset)."""


@dataclass(frozen=True, slots=True)
class ActionAvailability:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ControlResult:
    action: ControlAction
    task_id: str
    turn_started: bool
    execution_id: str | None = None
    client_action_id: str | None = None
    duplicate: bool = False
    deferred: bool | None = None


class TaskControlService:
    """HTTP-facing lifecycle controls that delegate every transition to the core."""

    def __init__(
        self,
        sessions: TaskSessionService,
        storage: SQLiteStorage,
        conversation: ConversationService,
        runner: TaskTurnRunner | None,
    ) -> None:
        self.sessions = sessions
        self.storage = storage
        self.conversation = conversation
        self.runner = runner
        self._task_locks: dict[str, Lock] = {}
        self._task_locks_guard = Lock()

    # ---- availability (the single decision point the UI renders) ----------

    def availability(self, record: TaskRecord) -> dict[ControlAction, ActionAvailability]:
        if not self.conversation.messaging_enabled:
            disabled = ActionAvailability(
                False, "Browser controls are disabled: AI_PLATFORM_WEB_ACTOR is not configured."
            )
            return {action: disabled for action in ControlAction}
        return {action: self._check(action, record) for action in ControlAction}

    def _check(self, action: ControlAction, record: TaskRecord) -> ActionAvailability:
        reason = self._blocker(action, record)
        if reason is None and action in TURN_ACTIONS and self.runner is None:
            reason = (
                "This server does not run agent turns (AI_PLATFORM_ENABLE_RUNNER is not set); "
                "use the CLI."
            )
        return ActionAvailability(reason is None, reason)

    def _blocker(self, action: ControlAction, record: TaskRecord) -> str | None:
        """Mirror the core's preconditions (the core stays the final arbiter)."""

        status = record.status
        writer = record.active_execution is not None
        workspace = self.sessions.workspaces.exists(record.task_id)
        if action is ControlAction.START:
            if writer or status in _ACTIVE_STATUSES:
                return "Task cannot be started because it is already running."
            if status is not TaskStatus.READY:
                return f"Task can only be started from READY (it is {status.value.upper()})."
            if workspace:
                return "Task already has a workspace; reset it before starting again."
            return None
        if action is ControlAction.PAUSE:
            if status is TaskStatus.COMPLETED:
                return "Task is already completed."
            if status is TaskStatus.READY or not workspace:
                return "Task has not been started."
            if record.pause_requested:
                return "Pause already requested; the agent stops at its next safe point."
            if status is TaskStatus.PAUSED_BY_HUMAN and record.active_execution is not (
                ExecutionKind.AGENT
            ):
                return "Task is already paused."
            return None
        if action is ControlAction.RESUME:
            if status is not TaskStatus.PAUSED_BY_HUMAN:
                return "Task is not paused."
            if writer:
                return OWNED
            return None
        if action is ControlAction.APPROVE:
            if status is not TaskStatus.WAITING_FOR_HUMAN:
                return "Task cannot be approved until it is waiting for human review."
            if record.verification_status is not VerificationStatus.PASSED:
                return "Task cannot be approved until verification has passed."
            if writer:
                return OWNED
            # Platform rule: a task cannot be approved while accepted human
            # instructions are still queued (approving would orphan them).
            if pending := self._pending_messages(record.task_id):
                return f"Wait until {pending} queued chat message(s) have been answered."
            return None
        if action is ControlAction.REJECT:
            if status is not TaskStatus.WAITING_FOR_HUMAN:
                return "Task can only be rejected while it is waiting for human review."
            if writer:
                return OWNED
            return None
        # RESET
        if writer:
            return "Task cannot be reset while an execution owns it; pause it first."
        if status is TaskStatus.READY and not workspace:
            return "Task is already in its initial READY state."
        return None

    def _pending_messages(self, task_id: str) -> int:
        return sum(
            1
            for message in self.storage.list_queued_messages(task_id)
            if message.status in {MessageStatus.QUEUED, MessageStatus.RUNNING}
        )

    # ---- turn-launching actions (async) -----------------------------------

    def start(self, task_id: str, client_action_id: str) -> ControlResult:
        return self._launch(
            ControlAction.START,
            task_id,
            client_action_id,
            lambda human, eid: self.sessions.prepare_start(task_id, human, execution_id=eid),
        )

    def resume(self, task_id: str, client_action_id: str, message: str | None) -> ControlResult:
        return self._launch(
            ControlAction.RESUME,
            task_id,
            client_action_id,
            lambda human, eid: self.sessions.prepare_resume(
                task_id, human, message, execution_id=eid
            ),
        )

    def reject(self, task_id: str, client_action_id: str, message: str) -> ControlResult:
        return self._launch(
            ControlAction.REJECT,
            task_id,
            client_action_id,
            lambda human, eid: self.sessions.prepare_reject(
                task_id, message, human, execution_id=eid
            ),
        )

    def _launch(
        self,
        action: ControlAction,
        task_id: str,
        client_action_id: str,
        prepare: Callable[..., PreparedTurn | TurnOutcome],
    ) -> ControlResult:
        human = self.conversation.web_identity()
        task = self.sessions.get_definition(task_id)
        # Deterministic per (task, action, key): a retry maps to the same execution.
        execution_id = str(uuid5(_IDEMPOTENCY_NAMESPACE, f"{task.id}:{action}:{client_action_id}"))
        with self._task_lock(task.id):
            if self.storage.execution_recorded(task.id, execution_id):
                return ControlResult(
                    action, task.id, True, execution_id, client_action_id, duplicate=True
                )
            if self.runner is None:
                raise RunnerUnavailableError(self._check(action, self._record(task.id)).reason)
            self._require(action, task.id)
            prepared = self._core(lambda: prepare(human, execution_id))
            if isinstance(prepared, TurnOutcome):
                # Only reject can get here: feedback recorded, another writer won the lock.
                raise ActionConflictError(f"{OWNED} The rejection feedback was recorded.")
            self.runner.run_prepared_turn(prepared)
        return ControlResult(action, task.id, True, execution_id, client_action_id)

    # ---- quick state actions (sync) ---------------------------------------

    def pause(self, task_id: str) -> ControlResult:
        human = self.conversation.web_identity()
        task = self.sessions.get_definition(task_id)
        with self._task_lock(task.id):
            self._require(ControlAction.PAUSE, task.id)
            deferred = self._core(lambda: self.sessions.pause(task.id, human))
        return ControlResult(ControlAction.PAUSE, task.id, False, deferred=deferred)

    def approve(self, task_id: str) -> ControlResult:
        human = self.conversation.web_identity()
        task = self.sessions.get_definition(task_id)
        with self._task_lock(task.id):
            self._require(ControlAction.APPROVE, task.id)
            self._core(lambda: self.sessions.approve(task.id, human))
        return ControlResult(ControlAction.APPROVE, task.id, False)

    def reset(self, task_id: str) -> ControlResult:
        human = self.conversation.web_identity()
        task = self.sessions.get_definition(task_id)
        with self._task_lock(task.id):
            self._require(ControlAction.RESET, task.id)
            self._core(lambda: self.sessions.reset(task.id, human))
        return ControlResult(ControlAction.RESET, task.id, False)

    # ---- helpers ----------------------------------------------------------

    def _record(self, task_id: str) -> TaskRecord:
        return self.sessions.get_session(task_id).record

    def _require(self, action: ControlAction, task_id: str) -> None:
        reason = self._blocker(action, self._record(task_id))
        if reason is not None:
            raise ActionConflictError(reason)

    @staticmethod
    def _core[T](operation: Callable[[], T]) -> T:
        """Run a core call, translating its errors into client-safe HTTP errors."""

        try:
            return operation()
        except (TaskNotFoundError, ExecutorUnavailableError):
            raise
        except TaskLockedError as error:
            raise ActionConflictError(OWNED) from error
        except (TaskSessionError, ApprovalError) as error:
            raise ActionConflictError(str(error)) from error

    def _task_lock(self, task_id: str) -> Lock:
        """Serialize this process's control requests per task (double clicks, retries)."""

        with self._task_locks_guard:
            return self._task_locks.setdefault(task_id, Lock())
