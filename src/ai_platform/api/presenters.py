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
from ai_platform.conversation import ConversationEntry, ConversationService
from ai_platform.events import Event, EventType
from ai_platform.models import (
    ExecutionKind,
    MessageStatus,
    QueuedMessage,
    TaskDefinition,
    TaskRecord,
)
from ai_platform.removal import RemovalAvailability
from ai_platform.sessions import TaskSession

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

    def message(self, entry: ConversationEntry) -> MessageResponse:
        event, delivery = entry.event, entry.delivery
        is_human = event.event_type is EventType.HUMAN_MESSAGE
        return MessageResponse(
            id=delivery.message_id if delivery else event.id,
            task_id=event.task_id,
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
            disposition=record.disposition,
            can_remove=removal.allowed,
            remove_disabled_reason=removal.reason,
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
