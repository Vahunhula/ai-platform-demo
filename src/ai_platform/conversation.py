"""Shared task conversation: durable browser message submission and conversation reads.

The conversation of a task is its append-only HUMAN_MESSAGE / AGENT_MESSAGE event
history. Browser messages add one ``message_queue`` row per message that records
only delivery state (idempotency key, QUEUED/RUNNING/COMPLETED/FAILED, execution
ID); the message text itself is the referenced HUMAN_MESSAGE event.
"""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from ai_platform.events import Event, EventType
from ai_platform.identity import HumanIdentity
from ai_platform.models import QueuedMessage, TaskRecord, TaskStatus
from ai_platform.sessions import TaskSessionError, TaskSessionService
from ai_platform.storage import SQLiteStorage
from ai_platform.workflow import ChecklistEvaluation, WorkflowArtifact, WorkflowPhase

# Event types that become their own chat-timeline entry beyond the plain
# HUMAN_MESSAGE/AGENT_MESSAGE conversation, so the human never has to leave
# Chat to see a phase result, a blocking question, or a command's outcome.
_PHASE_RESULT_EVENTS = frozenset({EventType.WORKFLOW_PHASE_OUTPUT_CREATED})
_WAITING_EVENTS = frozenset({EventType.WORKFLOW_PHASE_WAITING_FOR_HUMAN})
_ACTIVITY_EVENTS = frozenset(
    {EventType.WORKFLOW_PHASE_STARTED, EventType.WORKFLOW_PHASE_CHANGED}
)
_COMMAND_EVENTS = frozenset(
    {EventType.COMMAND_INVOKED, EventType.COMMAND_SUCCEEDED, EventType.COMMAND_FAILED}
)

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


@dataclass(frozen=True, slots=True)
class ChatItem:
    """One entry on the user-facing Chat timeline.

    Chat is a projection over durable domain data, never a second copy of it:
    ``kind`` says which durable source this entry renders (a conversation
    message, a workflow artifact, a waiting-for-human gate outcome, a phase
    transition, or a command result); only the fields that kind needs are
    populated. The presenter (never the browser) turns this into public text.
    """

    kind: str
    event: Event
    delivery: QueuedMessage | None = None
    artifact: WorkflowArtifact | None = None
    checklist: ChecklistEvaluation | None = None


def _build_chat_items(
    events: list[Event],
    deliveries: dict[int, QueuedMessage],
    artifacts_by_id: dict[str, WorkflowArtifact],
    evaluations_by_phase: dict[WorkflowPhase, list[ChecklistEvaluation]],
) -> list[ChatItem]:
    """Merge durable events, artifacts, and gate evaluations into one timeline.

    A checklist evaluation has no direct foreign key to the gate/waiting event
    it belongs to, but ``_persist_gate`` always creates exactly one evaluation
    immediately before emitting the matching WORKFLOW_PHASE_GATE_EVALUATED (and
    optionally WORKFLOW_PHASE_WAITING_FOR_HUMAN / WORKFLOW_PHASE_CHANGED) event
    for that phase, so walking both lists in lockstep per phase pairs them
    correctly even across multiple reruns of the same phase.
    """

    items: list[ChatItem] = []
    cursor: dict[WorkflowPhase, int] = {}
    last_evaluation: dict[WorkflowPhase, ChecklistEvaluation | None] = {}

    def _advance(phase: WorkflowPhase) -> None:
        bucket = evaluations_by_phase.get(phase, [])
        index = cursor.get(phase, 0)
        last_evaluation[phase] = bucket[index] if index < len(bucket) else None
        cursor[phase] = index + 1

    for event in events:
        event_type = event.event_type
        if event_type is EventType.HUMAN_MESSAGE:
            delivery = deliveries.get(event.sequence_id)
            items.append(ChatItem("human_message", event, delivery=delivery))
        elif event_type is EventType.AGENT_MESSAGE:
            delivery = deliveries.get(event.sequence_id)
            items.append(ChatItem("agent_message", event, delivery=delivery))
        elif event_type in _PHASE_RESULT_EVENTS:
            artifact = artifacts_by_id.get(str(event.metadata.get("artifact_id", "")))
            items.append(ChatItem("phase_result", event, artifact=artifact))
        elif event_type is EventType.WORKFLOW_PHASE_GATE_EVALUATED:
            # Chat-silent: its outcome is what the following WAITING_FOR_HUMAN
            # or PHASE_CHANGED event reports. Still advances the per-phase
            # evaluation cursor so that event can attach the right evidence.
            phase_value = event.metadata.get("phase")
            if phase_value and "score" in event.metadata:
                _advance(WorkflowPhase(phase_value))
        elif event_type in _WAITING_EVENTS:
            phase_value = event.metadata.get("phase")
            phase = WorkflowPhase(phase_value) if phase_value else None
            checklist = last_evaluation.get(phase) if phase else None
            items.append(ChatItem("human_input_required", event, checklist=checklist))
        elif event_type is EventType.WORKFLOW_PHASE_CHANGED:
            from_phase_value = event.metadata.get("from_phase")
            checklist = (
                last_evaluation.get(WorkflowPhase(from_phase_value)) if from_phase_value else None
            )
            items.append(ChatItem("platform_activity", event, checklist=checklist))
        elif event_type in _ACTIVITY_EVENTS:
            items.append(ChatItem("platform_activity", event))
        elif event_type in _COMMAND_EVENTS:
            items.append(ChatItem("command_result", event))
    return items


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

    def list_chat_items(self, task_id: str) -> list[ChatItem]:
        """Return the full Chat projection: conversation plus workflow narration.

        Everything a human needs to read or respond to (phase output, a
        waiting-for-human question, a command's result) belongs here so Chat
        never requires a trip to Activity/Trace; low-level diagnostics stay
        out of this projection and remain Activity-only.
        """

        session = self.sessions.get_session(task_id)
        task_id = session.definition.id
        deliveries = {
            message.event_sequence_id: message
            for message in self.storage.list_queued_messages(task_id)
        }
        artifacts_by_id = {
            artifact.artifact_id: artifact
            for artifact in self.storage.list_workflow_artifacts(task_id)
        }
        evaluations_by_phase: dict[WorkflowPhase, list[ChecklistEvaluation]] = {}
        for evaluation in self.storage.list_checklist_evaluations(task_id):
            evaluations_by_phase.setdefault(evaluation.phase, []).append(evaluation)
        return _build_chat_items(session.events, deliveries, artifacts_by_id, evaluations_by_phase)

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
