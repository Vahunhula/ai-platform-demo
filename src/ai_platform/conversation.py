"""Shared task conversation: durable browser message submission and conversation reads.

The conversation of a task is its append-only HUMAN_MESSAGE / AGENT_MESSAGE event
history. Browser messages add one ``message_queue`` row per message that records
only delivery state (idempotency key, QUEUED/RUNNING/COMPLETED/FAILED, execution
ID); the message text itself is the referenced HUMAN_MESSAGE event.
"""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from ai_platform.events import Event
from ai_platform.identity import HumanIdentity
from ai_platform.models import QueuedMessage, TaskRecord, TaskStatus
from ai_platform.sessions import TaskSessionError, TaskSessionService
from ai_platform.storage import SQLiteStorage

# States in which a browser message can be answered by a (possibly queued) agent turn.
ACCEPTING_STATUSES = frozenset(
    {
        TaskStatus.WAITING_FOR_HUMAN,
        TaskStatus.ANALYZING,
        TaskStatus.IMPLEMENTING,
        TaskStatus.VERIFYING,
    }
)

_REJECTION_REASONS = {
    TaskStatus.READY: "has not been started yet; start it first",
    TaskStatus.PAUSED_BY_HUMAN: "is paused by a human; resume it first",
    TaskStatus.COMPLETED: "is completed",
    TaskStatus.FAILED: "has failed; inspect or reset it first",
}


class MessageNotAcceptedError(TaskSessionError):
    """The task's current state cannot receive a new message."""


class IdempotencyConflictError(TaskSessionError):
    """A client message ID was reused for different message content."""


@dataclass(frozen=True, slots=True)
class ConversationEntry:
    """One conversation message: its event, plus delivery state for browser messages."""

    event: Event
    delivery: QueuedMessage | None


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """Outcome of a browser message submission."""

    message: QueuedMessage
    created: bool


class ConversationService:
    """Browser-facing conversation operations on top of TaskSessionService."""

    def __init__(
        self,
        sessions: TaskSessionService,
        storage: SQLiteStorage,
        *,
        on_submitted: Callable[[], None] | None = None,
    ) -> None:
        self.sessions = sessions
        self.storage = storage
        self.on_submitted = on_submitted

    @staticmethod
    def acceptance(record: TaskRecord) -> str | None:
        """Return None when a task can receive a browser message, else a reason."""

        if record.status in ACCEPTING_STATUSES:
            return None
        reason = _REJECTION_REASONS.get(record.status, f"is {record.status.value.upper()}")
        return f"{record.task_id} {reason}."

    def list_messages(self, task_id: str) -> list[ConversationEntry]:
        """Return the durable conversation in event-sequence order."""

        session = self.sessions.get_session(task_id)
        deliveries = {
            message.event_sequence_id: message
            for message in self.storage.list_queued_messages(session.definition.id)
        }
        return [
            ConversationEntry(event, deliveries.get(event.sequence_id or 0))
            for event in session.conversation
        ]

    def submit(
        self,
        task_id: str,
        content: str,
        client_message_id: str,
        human: HumanIdentity,
    ) -> SubmitResult:
        """Durably record a message and queue its agent turn, once per author and key.

        ``human`` is the authenticated author resolved by the HTTP layer; the
        idempotency key is scoped to it, so two users' keys never collide.
        """

        task = self.sessions.get_definition(task_id)
        normalized = self.sessions.validate_message(content)

        existing = self.storage.get_queued_message(task.id, human.actor_id, client_message_id)
        if existing is not None:
            return SubmitResult(self._same_content(existing, normalized), created=False)

        record = self.sessions.get_session(task.id).record
        if (reason := self.acceptance(record)) is not None:
            raise MessageNotAcceptedError(reason)

        message_id = str(uuid4())
        event = self.sessions.human_message_event(
            task.id,
            normalized,
            human,
            {"message_id": message_id, "channel": "web"},
        )
        stored = self.storage.enqueue_message(
            event,
            message_id=message_id,
            client_message_id=client_message_id,
            display_name=human.display_name,
            accepting_statuses=ACCEPTING_STATUSES,
        )
        if stored is None:  # the task changed state concurrently (e.g. approved elsewhere)
            latest = self.sessions.get_session(task.id).record
            raise MessageNotAcceptedError(self.acceptance(latest) or f"{task.id} changed state.")
        message, created = stored
        if not created:
            message = self._same_content(message, normalized)
        elif self.on_submitted is not None:
            self.on_submitted()
        return SubmitResult(message, created)

    def _same_content(self, message: QueuedMessage, content: str) -> QueuedMessage:
        event = self.storage.get_event(message.task_id, message.event_sequence_id)
        if event is None or event.metadata.get("message") != content:
            raise IdempotencyConflictError(
                "client_message_id was already used for a different message"
            )
        return message
