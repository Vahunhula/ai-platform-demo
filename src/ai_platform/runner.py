"""In-process runner for browser-initiated agent turns, all through the shared core.

It runs two kinds of work, never more than one turn per task at a time:

- queued chat messages (below), claimed from the durable ``message_queue``;
- lifecycle turns (start/resume/reject) that an HTTP request already prepared
  via ``TaskSessionService.prepare_*`` — the writer lock is held before the
  request returns, so they need no queue (``run_prepared_turn``).

Correctness does not depend on this process's memory:

- every accepted message is a durable ``message_queue`` row before the API replies;
- a message is claimed with an atomic SQL update (oldest QUEUED first, and never
  while another message of the same task is RUNNING);
- the turn itself runs through ``TaskSessionService.continue_conversation``, which
  takes the existing one-writer workspace lock. If the lock is not available the
  claim is returned to the queue.

Run at most one runner-enabled API process per runtime.
"""

import logging
from threading import Event as ThreadEvent
from threading import Lock, Thread
from uuid import uuid4

from ai_platform.identity import HumanIdentity
from ai_platform.models import MessageStatus, QueuedMessage, TaskStatus
from ai_platform.sessions import PreparedTurn, TaskSessionError, TaskSessionService
from ai_platform.storage import SQLiteStorage

logger = logging.getLogger(__name__)

# The previous turn is still in progress (or a pause holds the task): keep waiting.
_HOLD_STATUSES = frozenset(
    {
        TaskStatus.ANALYZING,
        TaskStatus.IMPLEMENTING,
        TaskStatus.VERIFYING,
        TaskStatus.PAUSED_BY_HUMAN,
    }
)
_INTERRUPTED = "The API process stopped before this message's agent turn finished."


class TaskTurnRunner:
    """Run queued browser messages FIFO per task, one agent turn at a time per task."""

    def __init__(
        self,
        sessions: TaskSessionService,
        storage: SQLiteStorage,
        *,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self.sessions = sessions
        self.storage = storage
        self.poll_interval_seconds = poll_interval_seconds
        self._wake = ThreadEvent()
        self._stopping = ThreadEvent()
        self._in_flight: set[str] = set()
        # Tasks whose lifecycle turn (start/resume/reject) is running in this process.
        self._control_turns: dict[str, int] = {}
        self._in_flight_lock = Lock()
        self._dispatcher: Thread | None = None
        self._workers: list[Thread] = []

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._dispatcher is not None:
            return
        self.fail_interrupted_messages()
        self._dispatcher = Thread(target=self._dispatch_loop, name="task-turn-runner", daemon=True)
        self._dispatcher.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop dispatching. A turn already running continues in its daemon thread."""

        self._stopping.set()
        self._wake.set()
        if self._dispatcher is not None:
            self._dispatcher.join(timeout)
        for worker in list(self._workers):
            worker.join(timeout)

    def wake(self) -> None:
        """Ask the dispatcher to look at the queue now instead of at the next poll."""

        self._wake.set()

    def run_prepared_turn(self, prepared: PreparedTurn) -> None:
        """Run a lifecycle turn whose writer lock the caller already holds, in background.

        Used by browser start/resume/reject: the HTTP request prepares the turn
        (checks, intent events, lock) and returns; the turn itself runs here. Queued
        chat messages of the task are picked up afterwards by the dispatcher.
        """

        task_id = prepared.task.id
        with self._in_flight_lock:
            self._control_turns[task_id] = self._control_turns.get(task_id, 0) + 1

        def work() -> None:
            try:
                self.sessions.run_prepared(prepared)
            except Exception:
                # The core already persisted TASK_FAILED and released the lock.
                logger.exception("Lifecycle turn %s failed", prepared.execution_id)
            finally:
                with self._in_flight_lock:
                    remaining = self._control_turns.get(task_id, 1) - 1
                    if remaining:
                        self._control_turns[task_id] = remaining
                    else:
                        self._control_turns.pop(task_id, None)
                self.wake()

        worker = Thread(target=work, name=f"task-lifecycle-{task_id}", daemon=True)
        self._workers = [thread for thread in self._workers if thread.is_alive()]
        self._workers.append(worker)
        worker.start()

    # ---- synchronous API (also used directly by tests) -------------------

    def run_pending(self) -> int:
        """Process every currently runnable message synchronously; return turns handled."""

        handled = 0
        for task_id in self.storage.tasks_with_queued_messages():
            while self.process_next(task_id):
                handled += 1
        return handled

    def process_next(self, task_id: str) -> bool:
        """Handle the oldest queued message of one task; return whether one was handled."""

        record = self.storage.get_task(task_id)
        if record is None or record.active_execution is not None:
            return False
        if record.status in _HOLD_STATUSES:
            return False
        if record.status is not TaskStatus.WAITING_FOR_HUMAN:
            return self._fail_next(task_id, record.status)

        execution_id = str(uuid4())
        message = self.storage.claim_next_message(task_id, execution_id)
        if message is None:
            return False
        return self._run_turn(message, execution_id)

    def fail_interrupted_messages(self) -> list[str]:
        """Fail RUNNING messages whose turn no longer holds the task's writer lock."""

        failed: list[str] = []
        with self._in_flight_lock:
            in_flight = set(self._in_flight) | set(self._control_turns)
        for message in self.storage.running_messages():
            if message.task_id in in_flight:
                continue
            record = self.storage.get_task(message.task_id)
            if record is not None and record.execution_id == message.execution_id:
                continue  # its writer lock is still held; stale-lock recovery owns that case
            if self.storage.finish_message(
                message.message_id, MessageStatus.FAILED, error=_INTERRUPTED
            ):
                failed.append(message.message_id)
        return failed

    # ---- internals -------------------------------------------------------

    def _run_turn(self, message: QueuedMessage, execution_id: str) -> bool:
        human = HumanIdentity(actor_id=message.actor_id, display_name=message.display_name)
        try:
            outcome = self.sessions.continue_conversation(
                message.task_id,
                human,
                execution_id=execution_id,
                through_sequence_id=message.event_sequence_id,
            )
        except TaskSessionError as error:
            self.storage.finish_message(message.message_id, MessageStatus.FAILED, error=str(error))
            return True
        except Exception:
            logger.exception("Agent turn for message %s failed unexpectedly", message.message_id)
            self.storage.finish_message(
                message.message_id,
                MessageStatus.FAILED,
                error="The agent turn failed unexpectedly; see the task trace.",
            )
            return True

        if not outcome.agent_started:
            # Another writer took the task first (e.g. a CLI turn): try again later.
            self.storage.requeue_message(message.message_id)
            return False
        final_status = (outcome.state or {}).get("status")
        if final_status == TaskStatus.FAILED.value:
            self.storage.finish_message(
                message.message_id,
                MessageStatus.FAILED,
                error="The agent turn ended with the task FAILED; see the task trace.",
            )
        else:
            self.storage.finish_message(message.message_id, MessageStatus.COMPLETED)
        return True

    def _fail_next(self, task_id: str, status: TaskStatus) -> bool:
        execution_id = str(uuid4())
        message = self.storage.claim_next_message(task_id, execution_id)
        if message is None:
            return False
        self.storage.finish_message(
            message.message_id,
            MessageStatus.FAILED,
            error=(
                f"{task_id} became {status.value.upper()} before this message's turn could "
                "run. The message remains in the conversation."
            ),
        )
        return True

    def _dispatch_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.fail_interrupted_messages()
                for task_id in self.storage.tasks_with_queued_messages():
                    self._start_worker(task_id)
            except Exception:
                logger.exception("Task turn dispatcher iteration failed")
            self._wake.wait(self.poll_interval_seconds)
            self._wake.clear()

    def _start_worker(self, task_id: str) -> None:
        with self._in_flight_lock:
            busy = task_id in self._in_flight or task_id in self._control_turns
            if busy or self._stopping.is_set():
                return
            self._in_flight.add(task_id)
        worker = Thread(
            target=self._work, args=(task_id,), name=f"task-turn-{task_id}", daemon=True
        )
        self._workers = [thread for thread in self._workers if thread.is_alive()]
        self._workers.append(worker)
        worker.start()

    def _work(self, task_id: str) -> None:
        try:
            while not self._stopping.is_set() and self.process_next(task_id):
                pass
        except Exception:
            logger.exception("Task turn worker for %s failed", task_id)
        finally:
            with self._in_flight_lock:
                self._in_flight.discard(task_id)
