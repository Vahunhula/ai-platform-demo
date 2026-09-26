"""Convert platform objects into public API contracts.

Every piece of data that leaves the HTTP API (JSON responses and SSE) passes
through this module, which applies two layers:

1. an allowlist of public event metadata keys (lock tokens, hostnames, PIDs,
   provider session IDs and absolute workspace paths are never copied);
2. ``PathRedactor``, which replaces this server's runtime locations inside the
   remaining text (e.g. pytest's ``rootdir:`` line) with stable placeholders.
   Persisted history is never rewritten; redaction happens on the way out.
"""

import re
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any

from ai_platform.api.schemas import (
    ActionState,
    BlockingCheckResponse,
    EventResponse,
    MessageResponse,
    MessagingState,
    ReadinessSummary,
    TaskActions,
    TaskDetailResponse,
    TaskListItem,
    VerificationResultResponse,
)
from ai_platform.auth import AuthenticatedUser
from ai_platform.config import Settings
from ai_platform.controls import READ_ONLY, ActionAvailability, ControlAction
from ai_platform.conversation import ChatItem, ConversationService
from ai_platform.events import ActorType, Event, EventType
from ai_platform.models import (
    ExecutionKind,
    MessageStatus,
    QueuedMessage,
    TaskDefinition,
    TaskRecord,
)
from ai_platform.removal import RemovalAvailability
from ai_platform.sessions import TaskSession
from ai_platform.workflow import (
    ArtifactKind,
    ChecklistEvaluation,
    ChecklistStatus,
    WorkflowArtifact,
    WorkflowPhase,
)

_PHASE_DISPLAY = {
    WorkflowPhase.BRAINSTORM: "Brainstorm",
    WorkflowPhase.PLAN: "Plan",
    WorkflowPhase.IMPLEMENTATION: "Implementation",
    WorkflowPhase.REVIEW: "Review",
    WorkflowPhase.HUMAN_REVIEW: "Human Review",
}


def _phase_label(phase: WorkflowPhase | None) -> str:
    if phase is None:
        return ""
    return _PHASE_DISPLAY.get(phase, phase.value.replace("_", " ").title())


def _bulleted(label: str, values: list[object]) -> str:
    if not values:
        return ""
    body = "\n".join(f"- {value}" for value in values)
    return f"\n\n{label}\n{body}"


def _render_artifact_body(kind: ArtifactKind, payload: dict[str, Any]) -> str:
    """Render one workflow artifact payload as readable Chat text (never raw JSON)."""

    if kind is ArtifactKind.BRAINSTORM_SUMMARY:
        return (
            str(payload.get("summary", ""))
            + _bulleted("Assumptions", payload.get("assumptions", []))
            + _bulleted("Options", payload.get("options", []))
            + _bulleted("Open questions", payload.get("questions", []))
        )
    if kind is ArtifactKind.PLAN:
        return (
            str(payload.get("summary", ""))
            + _bulleted("Files likely affected", payload.get("files", []))
            + _bulleted("Steps", payload.get("steps", []))
            + _bulleted("Validation", payload.get("tests", []))
            + _bulleted("Risks", payload.get("risks", []))
            + _bulleted("Open questions", payload.get("open_questions", []))
        )
    if kind is ArtifactKind.IMPLEMENTATION_SUMMARY:
        return (
            str(payload.get("summary", ""))
            + _bulleted("Changed", payload.get("files_changed", []))
            + _bulleted("Verification", payload.get("tests_run", []))
            + _bulleted("Known issues", payload.get("known_issues", []))
        )
    if kind is ArtifactKind.REVIEW_REPORT:
        assessments = [
            f"Requirements: {payload.get('requirements_assessment', '')}",
            f"Tests: {payload.get('test_assessment', '')}",
            f"Conventions: {payload.get('convention_assessment', '')}",
        ]
        return (
            str(payload.get("summary", ""))
            + _bulleted("Critical findings", payload.get("critical_findings", []))
            + _bulleted("Major findings", payload.get("major_findings", []))
            + _bulleted("Minor findings", payload.get("minor_findings", []))
            + _bulleted("Assessment", assessments)
        )
    if kind is ArtifactKind.HUMAN_REVIEW_DECISION:
        text = f"Decision: {payload.get('decision', '')}"
        feedback = payload.get("feedback")
        if feedback:
            text += f"\n\n{feedback}"
        target = payload.get("target_phase")
        if target:
            text += f"\n\nNext phase: {str(target).replace('_', ' ').title()}"
        return text
    return str(payload)

_COMMON_PUBLIC_METADATA = {
    "attempt",
    "turn_attempt",
    "tier_attempt",
    "continuation",
    "difficulty",
    "tier",
    "model",
    "reason",
    "previous_tier",
    "previous_model",
    "new_tier",
    "new_model",
    "failed_attempt_count",
    "display_name",
    "from",
    "to",
    "summary",
    "message",
    "message_id",
    "channel",
    "error",
    "fatal",
    "cancelled",
    "change_type",
    "files",
    "deferred",
    "active_execution",
    "exit_code",
    "duration_seconds",
    "timed_out",
    "started_at",
    "finished_at",
    "stdout",
    "stderr",
    "workspace_retained",
    "sdk_version",
    "authentication_method",
    "activity",
    "tool",
    "is_error",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "turns",
    "duration_ms",
    "duration_api_ms",
    "total_cost_usd",
    "models",
    "repository_id",
    "repository_slug",
    "base_branch",
    "source_commit",
    "assignee_user_id",
    "assignee_username",
    "from_phase",
    "to_phase",
    "transition_mode",
    "verification_mode",
    "task_specific_targets",
    "task_specific_passed",
    "task_specific_passed_tests",
    "broad_regression_passed",
    "baseline_warning_count",
    "new_regression_count",
    "pre_existing_failures",
    "fixed_failures",
    "new_failures",
    "blocking_failures",
    "warnings",
    "baseline_cached",
    "baseline_identity",
    "old_selection",
    "new_selection",
    "phase",
    "requested_selection",
    "effective_selection",
    "provider",
    "resolution_source",
    "workflow_phase",
    "command",
    "arguments",
    "client_command_id",
    "task_status",
    "result_category",
    "command_result",
    "namespace",
    "classification",
    "executor",
    "disposition",
}
_TEST_EVENTS = {EventType.TEST_STARTED, EventType.TEST_PASSED, EventType.TEST_FAILED}
# A path component continues with these characters; a placeholder must not cut one in half.
_PATH_CONTINUES = r"(?![A-Za-z0-9._-])"


class PathRedactor:
    """Replace this server's runtime paths in public text with placeholders.

    - ``<workspace-root>/<TASK-ID>...`` → ``<task-workspace>...``
    - the workspace root itself → ``<workspace-root>``
    - the platform data directory → ``<platform-data>``
    - the platform source checkout → ``<platform-root>``

    Only these configured locations are touched; relative paths and unrelated
    absolute paths stay as they are.
    """

    def __init__(self, settings: Settings) -> None:
        workspace_roots = self._variants(settings.workspace_root)
        self._task_workspace = [
            re.compile(re.escape(root) + r"/[A-Za-z0-9._-]+" + _PATH_CONTINUES)
            for root in workspace_roots
        ]
        prefixes = [(root, "<workspace-root>") for root in workspace_roots]
        prefixes += [(path, "<platform-data>") for path in self._variants(settings.data_dir)]
        prefixes += [(path, "<platform-root>") for path in self._variants(settings.project_root)]
        prefixes.sort(key=lambda item: len(item[0]), reverse=True)
        self._prefixes = [
            (re.compile(re.escape(prefix) + _PATH_CONTINUES), label) for prefix, label in prefixes
        ]

    @staticmethod
    def _variants(path: Path) -> list[str]:
        values = {str(path).rstrip("/"), str(path.resolve()).rstrip("/")}
        return sorted(value for value in values if value not in {"", "/"})

    def text(self, value: str) -> str:
        for pattern in self._task_workspace:
            value = pattern.sub("<task-workspace>", value)
        for pattern, label in self._prefixes:
            value = pattern.sub(label, value)
        return value

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {key: self.value(item) for key, item in value.items()}
        return value


class Presenter:
    """Build public response models from core objects."""

    def __init__(
        self,
        settings: Settings,
        display_names: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self.redactor = PathRedactor(settings)
        self._load_names = display_names or dict
        self._names: dict[str, str] = {}
        self._names_loaded_at = float("-inf")

    def _display_name(self, event: Event) -> str:
        """Name recorded with the event; else the provisioned user's current name.

        Historical events are never rewritten and ``actor_id`` is always returned
        alongside, so auditability never depends on a mutable display name.
        """

        recorded = event.metadata.get("display_name")
        if isinstance(recorded, str) and recorded.strip():
            return recorded
        if monotonic() - self._names_loaded_at > 5:
            self._names = self._load_names()
            self._names_loaded_at = monotonic()
        return self._names.get(event.actor_id, event.actor_id)

    def public_metadata(self, event: Event) -> dict[str, Any]:
        metadata = {
            key: value for key, value in event.metadata.items() if key in _COMMON_PUBLIC_METADATA
        }
        if event.event_type is EventType.FILE_CHANGED and "path" in event.metadata:
            metadata["path"] = event.metadata["path"]
        if event.event_type in _TEST_EVENTS and "command" in event.metadata:
            metadata["command"] = event.metadata["command"]
        return self.redactor.value(metadata)

    def event(self, event: Event) -> EventResponse:
        return EventResponse(
            sequence_id=event.sequence_id or 0,
            timestamp=event.timestamp,
            event_type=event.event_type.value,
            actor_type=event.actor_type.value,
            actor_id=event.actor_id,
            actor_display_name=self._display_name(event),
            execution_id=event.metadata.get("execution_id"),
            metadata=self.public_metadata(event),
        )

    def chat_item(self, item: ChatItem) -> MessageResponse:
        """Build one public Chat-timeline entry from its durable ``ChatItem``."""

        builders = {
            "human_message": self._conversation_message,
            "agent_message": self._conversation_message,
            "phase_result": self._phase_result,
            "human_input_required": self._human_input_required,
            "platform_activity": self._platform_activity,
            "command_result": self._command_result,
        }
        return builders[item.kind](item)

    def _conversation_message(self, item: ChatItem) -> MessageResponse:
        event, delivery = item.event, item.delivery
        is_human = item.kind == "human_message"
        return MessageResponse(
            id=delivery.message_id if delivery else event.id,
            task_id=event.task_id,
            type=item.kind,
            role="human" if is_human else "agent",
            actor_id=event.actor_id,
            actor_display_name=self._display_name(event),
            content=self.redactor.text(str(event.metadata.get("message", ""))),
            timestamp=event.timestamp,
            sequence_id=event.sequence_id or 0,
            turn_id=(delivery.execution_id if delivery else event.metadata.get("execution_id")),
            status=delivery.status.value.upper() if delivery else None,
            error=self.redactor.text(delivery.error) if delivery and delivery.error else None,
            client_message_id=delivery.client_message_id if delivery else None,
            channel=str(event.metadata["channel"]) if "channel" in event.metadata else None,
        )

    def _artifact_author(self, artifact: WorkflowArtifact) -> str:
        if artifact.created_by_type is ActorType.AGENT:
            return "Claude"
        if monotonic() - self._names_loaded_at > 5:
            self._names = self._load_names()
            self._names_loaded_at = monotonic()
        return self._names.get(artifact.created_by, artifact.created_by)

    def _phase_result(self, item: ChatItem) -> MessageResponse:
        event, artifact = item.event, item.artifact
        phase_value = event.metadata.get("phase")
        phase = (
            WorkflowPhase(phase_value) if phase_value else (artifact.phase if artifact else None)
        )
        if artifact is None:
            # Defensive only: events are append-only, so a referenced artifact
            # should always resolve; this guards a legacy/corrupted history.
            return MessageResponse(
                id=event.id,
                task_id=event.task_id,
                type="phase_result",
                role="agent",
                actor_id=event.actor_id,
                actor_display_name="Claude",
                title=f"Claude · {_phase_label(phase)}",
                content="This phase's output is no longer available.",
                timestamp=event.timestamp,
                sequence_id=event.sequence_id or 0,
                turn_id=event.metadata.get("execution_id"),
                workflow_phase=phase,
                status=None,
                error=None,
                client_message_id=None,
                channel=None,
            )
        author = self._artifact_author(artifact)
        return MessageResponse(
            id=event.id,
            task_id=event.task_id,
            type="phase_result",
            role="agent" if artifact.created_by_type is ActorType.AGENT else "human",
            actor_id=artifact.created_by,
            actor_display_name=author,
            title=f"{author} · {_phase_label(artifact.phase)}",
            content=self.redactor.text(_render_artifact_body(artifact.kind, artifact.payload)),
            timestamp=event.timestamp,
            sequence_id=event.sequence_id or 0,
            turn_id=event.metadata.get("execution_id"),
            workflow_phase=artifact.phase,
            artifact_kind=artifact.kind,
            artifact_version=artifact.version,
            logical_model=artifact.logical_model,
            concrete_model=artifact.concrete_model,
            status=None,
            error=None,
            client_message_id=None,
            channel=None,
        )

    def _blocking_checks(
        self, checklist: ChecklistEvaluation | None, keys: list[str]
    ) -> list[BlockingCheckResponse]:
        by_key = {item.key: item for item in checklist.items} if checklist else {}
        checks = []
        for key in keys:
            found = by_key.get(key)
            if found is not None:
                checks.append(
                    BlockingCheckResponse(
                        key=found.key,
                        label=found.label,
                        status=found.status.value,
                        evidence=self.redactor.text(found.evidence),
                    )
                )
            else:
                checks.append(
                    BlockingCheckResponse(
                        key=key,
                        label=key.replace("_", " ").capitalize(),
                        status=ChecklistStatus.NEEDS_HUMAN.value,
                        evidence="",
                    )
                )
        return checks

    def _human_input_required(self, item: ChatItem) -> MessageResponse:
        event = item.event
        metadata = event.metadata
        phase_value = metadata.get("phase")
        phase = WorkflowPhase(phase_value) if phase_value else None
        phase_label = _phase_label(phase)
        score = metadata.get("score")
        keys = [*metadata.get("blocking_failures", []), *metadata.get("blocking_needs_human", [])]
        checks = self._blocking_checks(item.checklist, [str(key) for key in keys])
        lines = [f"The {phase_label} phase cannot proceed automatically."]
        if checks:
            lines.append("")
            lines.append("Questions:")
            for index, check in enumerate(checks, start=1):
                evidence = f" — {check.evidence}" if check.evidence else ""
                lines.append(f"{index}. {check.label}{evidence}")
        if score is not None:
            lines.append("")
            lines.append(f"Readiness: {float(score):.0f}%")
        return MessageResponse(
            id=event.id,
            task_id=event.task_id,
            type="human_input_required",
            role="platform",
            actor_id=event.actor_id,
            actor_display_name="Platform",
            title=f"Needs your input · {phase_label}",
            content="\n".join(lines),
            timestamp=event.timestamp,
            sequence_id=event.sequence_id or 0,
            turn_id=metadata.get("execution_id"),
            workflow_phase=phase,
            readiness_score=float(score) if score is not None else None,
            requires_human_input=True,
            blocking_checks=checks,
            status=None,
            error=None,
            client_message_id=None,
            channel=None,
        )

    def _platform_activity(self, item: ChatItem) -> MessageResponse:
        event = item.event
        metadata = event.metadata
        phase: WorkflowPhase | None = None
        if event.event_type is EventType.WORKFLOW_PHASE_STARTED:
            phase = WorkflowPhase(metadata["phase"])
            content = f"{_phase_label(phase)} phase started"
        else:  # WORKFLOW_PHASE_CHANGED
            from_phase = WorkflowPhase(metadata["from_phase"])
            to_phase = WorkflowPhase(metadata["to_phase"])
            phase = to_phase
            from_label, to_label = _phase_label(from_phase), _phase_label(to_phase)
            if metadata.get("transition_mode") == "AUTOMATIC":
                if item.checklist is not None:
                    score = item.checklist.readiness.score
                    content = (
                        f"{from_label} passed readiness gate · {score:.0f}%\nMoving to {to_label}…"
                    )
                else:
                    content = f"{from_label} passed its readiness gate.\nMoving to {to_label}…"
            else:
                content = f"Moved from {from_label} to {to_label}."
        return MessageResponse(
            id=event.id,
            task_id=event.task_id,
            type="platform_activity",
            role="platform",
            actor_id=event.actor_id,
            actor_display_name="Platform",
            content=content,
            timestamp=event.timestamp,
            sequence_id=event.sequence_id or 0,
            turn_id=metadata.get("execution_id"),
            workflow_phase=phase,
            status=None,
            error=None,
            client_message_id=None,
            channel=None,
        )

    def _command_result(self, item: ChatItem) -> MessageResponse:
        event = item.event
        metadata = event.metadata
        command = str(metadata.get("command", "/command"))
        invoked = event.event_type is EventType.COMMAND_INVOKED
        if invoked:
            arguments = str(metadata.get("arguments", ""))
            content = f"{command}{' ' + arguments if arguments else ''}"
            role, actor_display_name = "human", self._display_name(event)
        else:
            content = str(metadata.get("command_result") or event.event_type.value)
            claude_namespace = metadata.get("namespace") == "claude"
            role = "platform"
            actor_display_name = "Claude command" if claude_namespace else "Platform"
        return MessageResponse(
            id=event.id,
            task_id=event.task_id,
            type="command_result",
            role=role,
            actor_id=event.actor_id,
            actor_display_name=actor_display_name,
            content=self.redactor.text(content),
            timestamp=event.timestamp,
            sequence_id=event.sequence_id or 0,
            turn_id=metadata.get("execution_id"),
            status=None,
            error=None,
            client_message_id=None,
            channel=None,
        )

    @staticmethod
    def task_item(definition: TaskDefinition, record: TaskRecord) -> TaskListItem:
        return TaskListItem(
            id=definition.id,
            title=definition.title,
            difficulty=definition.difficulty.value.upper(),
            status=record.status.value.upper(),
            workflow_phase=record.workflow_phase,
            model_tier=(record.selected_tier.value if record.selected_tier else None),
            writer=_writer(record),
            disposition=record.disposition,
            updated_at=record.updated_at,
        )

    def task_detail(
        self,
        session: TaskSession,
        user: AuthenticatedUser,
        queued_messages: int,
        actions: dict[ControlAction, ActionAvailability],
        *,
        removal: RemovalAvailability,
        latest_readiness: ReadinessSummary | None = None,
    ) -> TaskDetailResponse:
        record = session.record
        reason = ConversationService.acceptance(record)
        if not user.role.can_modify_tasks:
            reason = READ_ONLY
        return TaskDetailResponse(
            **self.task_item(session.definition, record).model_dump(),
            description=session.definition.description,
            acceptance_criteria=session.definition.acceptance_criteria,
            model_name=record.selected_model,
            workspace_id=session.definition.id if record.workspace_path else None,
            current_attempt=record.attempt,
            verification_status=record.verification_status.value.upper(),
            verification_result=self._verification_result(session.events),
            agent_working=record.active_execution is ExecutionKind.AGENT,
            pause_requested=record.pause_requested,
            queued_messages=queued_messages,
            messaging=MessagingState(accepting=reason is None, reason=reason),
            actions=TaskActions(
                **{
                    action.value: ActionState(
                        allowed=state.allowed,
                        reason=self.redactor.text(state.reason) if state.reason else None,
                    )
                    for action, state in actions.items()
                }
            ),
            created_at=record.created_at,
            default_model_selection=record.default_model_selection,
            latest_readiness=latest_readiness,
            can_remove=removal.allowed,
            remove_disabled_reason=removal.reason,
            repository_id=record.repository_id,
            base_branch=record.base_branch,
        )

    def _verification_result(self, events: list[Event]) -> VerificationResultResponse | None:
        for event in reversed(events):
            if event.event_type not in {EventType.TEST_PASSED, EventType.TEST_FAILED}:
                continue
            metadata = self.redactor.value(event.metadata)
            return VerificationResultResponse(
                status="passed" if event.event_type is EventType.TEST_PASSED else "failed",
                sequence_id=event.sequence_id or 0,
                timestamp=event.timestamp,
                exit_code=metadata.get("exit_code"),
                duration_seconds=metadata.get("duration_seconds"),
                timed_out=metadata.get("timed_out"),
                stdout=metadata.get("stdout"),
                stderr=metadata.get("stderr"),
                error=metadata.get("error"),
                verification_mode=metadata.get("verification_mode"),
                task_specific_targets=metadata.get("task_specific_targets", []),
                task_specific_passed=metadata.get("task_specific_passed"),
                task_specific_passed_tests=metadata.get("task_specific_passed_tests"),
                broad_regression_passed=metadata.get("broad_regression_passed"),
                baseline_warning_count=metadata.get("baseline_warning_count", 0),
                new_regression_count=metadata.get("new_regression_count", 0),
                pre_existing_failures=metadata.get("pre_existing_failures", []),
                fixed_failures=metadata.get("fixed_failures", []),
                new_failures=metadata.get("new_failures", []),
            )
        return None


def _writer(record: TaskRecord) -> str | None:
    if record.active_execution is None:
        return None
    return record.execution_actor_id or record.active_execution.value


def queued_count(messages: list[QueuedMessage]) -> int:
    return sum(1 for message in messages if message.status is MessageStatus.QUEUED)
