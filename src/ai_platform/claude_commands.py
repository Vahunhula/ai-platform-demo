"""Allowlisted Claude capability namespace; never a Claude CLI or shell proxy."""

from __future__ import annotations

import importlib.metadata
import shlex
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from ai_platform.auth import AuthenticatedUser
from ai_platform.commands import (
    CommandArgumentError,
    CommandParseError,
    CommandPermission,
    CommandResult,
    CommandUnavailableError,
    UnknownCommandError,
)
from ai_platform.config import Settings
from ai_platform.controls import PermissionDeniedError
from ai_platform.events import ActorType, Event, EventType
from ai_platform.executors.claude import READ_ONLY_PHASES
from ai_platform.models import TaskRecord
from ai_platform.sessions import MAX_MESSAGE_LENGTH, TaskSessionService
from ai_platform.storage import SQLiteStorage
from ai_platform.workflow import WorkflowPhase


class ClaudeCommandClassification(StrEnum):
    NATIVE = "NATIVE"
    ADAPTED = "ADAPTED"
    DISABLED = "DISABLED"
    FUTURE_INFRA_ONLY = "FUTURE_INFRA_ONLY"


@dataclass(frozen=True, slots=True)
class ParsedClaudeCommand:
    name: str
    arguments: tuple[str, ...]

    @property
    def argument_text(self) -> str:
        return " ".join(self.arguments)


def parse_claude_command(text: str) -> ParsedClaudeCommand | None:
    """Recognize only a leading ``claude/<allowlisted-name>`` expression."""

    normalized = text.strip()
    if not normalized.lower().startswith("claude/"):
        return None
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError as error:
        raise CommandParseError(f"Malformed Claude command: {error}") from error
    token = tokens[0] if tokens else ""
    if not token.lower().startswith("claude/"):
        return None
    name = token[7:].lower()
    if not name or "/" in name or not name.replace("-", "").isalnum():
        raise CommandParseError("Claude command must be claude/<name>")
    return ParsedClaudeCommand(name, tuple(tokens[1:]))


ClaudeHandler = Callable[["ClaudeCommandInvocation"], CommandResult]


@dataclass(frozen=True, slots=True)
class ClaudeCommandDefinition:
    name: str
    description: str
    usage: str
    classification: ClaudeCommandClassification
    required_permission: CommandPermission
    executor_capability: str
    handler: ClaudeHandler | None
    disabled_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ClaudeCommandMetadata:
    namespace: str
    command: str
    description: str
    usage: str
    classification: ClaudeCommandClassification
    required_permission: CommandPermission
    executor_capability: str
    available: bool
    disabled_reason: str | None


@dataclass(frozen=True, slots=True)
class ClaudeCommandInvocation:
    task: TaskRecord
    parsed: ParsedClaudeCommand
    user: AuthenticatedUser
    client_command_id: str


class ClaudeCommandRegistry:
    """Stable, backend-owned Claude capability catalog."""

    def __init__(self) -> None:
        self._commands: dict[str, ClaudeCommandDefinition] = {}

    def register(self, definition: ClaudeCommandDefinition) -> None:
        if definition.name in self._commands:
            raise ValueError(f"Duplicate Claude command: claude/{definition.name}")
        self._commands[definition.name] = definition

    def get(self, name: str) -> ClaudeCommandDefinition | None:
        return self._commands.get(name.lower().removeprefix("claude/"))

    def resolve(self, name: str) -> ClaudeCommandDefinition:
        definition = self.get(name)
        if definition is None:
            shown = name if name.startswith("claude/") else f"claude/{name}"
            raise UnknownCommandError(f'Unknown Claude command "{shown}"')
        return definition

    def list(self) -> tuple[ClaudeCommandDefinition, ...]:
        return tuple(self._commands.values())


class ClaudeCapabilityBridge:
    """Safe adaptations over platform executor state and audited SDK metadata."""

    def __init__(self, settings: Settings, storage: SQLiteStorage) -> None:
        self.settings = settings
        self.storage = storage

    @staticmethod
    def sdk_version() -> str | None:
        try:
            return importlib.metadata.version("claude-agent-sdk")
        except importlib.metadata.PackageNotFoundError:
            return None

    def status(self, record: TaskRecord) -> dict[str, Any]:
        has_resume_metadata = any(
            bool(event.metadata.get("session_id"))
            for event in self.storage.get_events(record.task_id)
            if event.event_type in {EventType.AGENT_COMPLETED, EventType.AGENT_FAILED}
        )
        if record.workflow_phase is WorkflowPhase.HUMAN_REVIEW:
            execution_mode = "disabled"
            execution_reason = "Human Review never starts AI automatically."
        elif record.workflow_phase in READ_ONLY_PHASES:
            execution_mode = "read_only"
            execution_reason = "This workflow phase permits only Read, Glob, and Grep."
        else:
            execution_mode = "workspace_write"
            execution_reason = None
        phase_preference = (
            None
            if record.workflow_phase is WorkflowPhase.HUMAN_REVIEW
            else self.storage.get_phase_model_preference(record.task_id, record.workflow_phase)
        )
        return {
            "provider": self.settings.executor.lower(),
            "sdk_version": self.sdk_version(),
            "logical_model": (
                phase_preference.model_selection.value
                if phase_preference
                else record.default_model_selection.value
            ),
            "concrete_model": record.selected_model,
            "agent_turn_active": record.agent_running,
            "workflow_phase": record.workflow_phase.value,
            "execution_mode": execution_mode,
            "execution_reason": execution_reason,
            "has_persisted_resume_metadata": has_resume_metadata,
        }


class ClaudeCommandService:
    """Task-aware orchestration for the explicit ``claude/`` namespace."""

    def __init__(
        self,
        sessions: TaskSessionService,
        storage: SQLiteStorage,
        settings: Settings,
    ) -> None:
        self.sessions = sessions
        self.storage = storage
        self.bridge = ClaudeCapabilityBridge(settings, storage)
        self.registry = ClaudeCommandRegistry()
        self._register()

    def _register(self) -> None:
        self.registry.register(
            ClaudeCommandDefinition(
                "help",
                "List audited Claude capabilities for this task.",
                "claude/help",
                ClaudeCommandClassification.ADAPTED,
                CommandPermission.VIEW,
                "registry_metadata",
                self._help,
            )
        )
        self.registry.register(
            ClaudeCommandDefinition(
                "status",
                "Show safe Claude executor state without provider credentials or paths.",
                "claude/status",
                ClaudeCommandClassification.ADAPTED,
                CommandPermission.VIEW,
                "platform_executor_status",
                self._status,
            )
        )
        disabled = (
            ("auth", "Authentication changes and credential inspection are forbidden."),
            ("config", "Global Claude configuration is outside task scope."),
            ("permissions", "Provider permission modes cannot override platform phase safety."),
            ("session-delete", "Arbitrary provider-session deletion is forbidden."),
        )
        for name, reason in disabled:
            self.registry.register(
                ClaudeCommandDefinition(
                    name,
                    reason,
                    f"claude/{name}",
                    ClaudeCommandClassification.DISABLED,
                    CommandPermission.MODIFY,
                    "unsupported_or_forbidden",
                    None,
                    reason,
                )
            )
        self.registry.register(
            ClaudeCommandDefinition(
                "mcp",
                "Inspect or alter Claude MCP integrations.",
                "claude/mcp",
                ClaudeCommandClassification.FUTURE_INFRA_ONLY,
                CommandPermission.MODIFY,
                "sdk_mcp_control",
                None,
                "MCP administration is reserved for future infrastructure tooling.",
            )
        )

    def metadata(self, task_id: str, user: AuthenticatedUser) -> list[ClaudeCommandMetadata]:
        record = self.sessions.get_session(task_id).record
        return [self._metadata(item, record, user) for item in self.registry.list()]

    def _metadata(
        self,
        definition: ClaudeCommandDefinition,
        record: TaskRecord,
        user: AuthenticatedUser,
    ) -> ClaudeCommandMetadata:
        reason = definition.disabled_reason
        if (
            reason is None
            and definition.required_permission is CommandPermission.MODIFY
            and not user.role.can_modify_tasks
        ):
            reason = "Developer access is required."
        if reason is None and record.removal_started_at is not None:
            reason = "Task removal is in progress."
        return ClaudeCommandMetadata(
            namespace="claude",
            command=f"claude/{definition.name}",
            description=definition.description,
            usage=definition.usage,
            classification=definition.classification,
            required_permission=definition.required_permission,
            executor_capability=definition.executor_capability,
            available=reason is None and definition.handler is not None,
            disabled_reason=reason,
        )

    def execute(
        self,
        task_id: str,
        command_text: str,
        user: AuthenticatedUser,
        client_command_id: str,
    ) -> CommandResult:
        record = self.sessions.get_session(task_id).record
        parsed: ParsedClaudeCommand | None = None
        name = self._attempted_name(command_text)
        definition: ClaudeCommandDefinition | None = None
        try:
            parsed = parse_claude_command(command_text)
            if parsed is None:
                raise CommandParseError("Input is not a Claude command")
            name = parsed.name
            definition = self.registry.resolve(name)
            self._audit(
                EventType.COMMAND_INVOKED,
                record,
                user,
                definition,
                client_command_id,
            )
            if (
                definition.required_permission is CommandPermission.MODIFY
                and not user.role.can_modify_tasks
            ):
                raise PermissionDeniedError("Developer access is required.")
            metadata = self._metadata(definition, record, user)
            if not metadata.available:
                raise CommandUnavailableError(
                    metadata.disabled_reason or "Claude command unavailable"
                )
            assert definition.handler is not None
            result = definition.handler(
                ClaudeCommandInvocation(record, parsed, user, client_command_id)
            )
        except Exception as error:
            if parsed is None or definition is None:
                self._audit_unknown(record, user, name, client_command_id)
            safe = (
                str(error)
                if isinstance(
                    error,
                    (
                        CommandArgumentError,
                        CommandParseError,
                        CommandUnavailableError,
                        PermissionDeniedError,
                        UnknownCommandError,
                    ),
                )
                else "Claude command failed unexpectedly"
            )
            self._audit_result(
                EventType.COMMAND_FAILED,
                self.storage.get_task(record.task_id) or record,
                user,
                definition,
                name,
                "",
                client_command_id,
                "failed",
                safe,
            )
            raise
        self._audit_result(
            EventType.COMMAND_SUCCEEDED,
            self.storage.get_task(record.task_id) or record,
            user,
            definition,
            name,
            "",
            client_command_id,
            "completed",
            result.message,
        )
        return result

    @staticmethod
    def _attempted_name(text: str) -> str:
        return text.strip().split(maxsplit=1)[0][:80].removeprefix("claude/") or "invalid"

    def _audit(
        self,
        event_type: EventType,
        record: TaskRecord,
        user: AuthenticatedUser,
        definition: ClaudeCommandDefinition,
        client_command_id: str,
    ) -> None:
        self._audit_result(
            event_type,
            record,
            user,
            definition,
            definition.name,
            "",
            client_command_id,
            "invoked",
            None,
        )

    def _audit_unknown(
        self,
        record: TaskRecord,
        user: AuthenticatedUser,
        name: str,
        client_command_id: str,
    ) -> None:
        self._audit_result(
            EventType.COMMAND_INVOKED,
            record,
            user,
            None,
            name,
            "",
            client_command_id,
            "invoked",
            None,
        )

    def _audit_result(
        self,
        event_type: EventType,
        record: TaskRecord,
        user: AuthenticatedUser,
        definition: ClaudeCommandDefinition | None,
        name: str,
        arguments: str,
        client_command_id: str,
        category: str,
        result: str | None,
    ) -> None:
        metadata: dict[str, Any] = {
            "namespace": "claude",
            "command": f"claude/{name}",
            "classification": (definition.classification.value if definition else "UNKNOWN"),
            "arguments": arguments[:MAX_MESSAGE_LENGTH],
            "client_command_id": client_command_id,
            "display_name": user.display_name,
            "task_status": record.status.value.upper(),
            "workflow_phase": record.workflow_phase.value,
            "result_category": category,
            "executor": self.bridge.settings.executor.lower(),
            "model": record.selected_model,
        }
        if result:
            metadata["command_result"] = result[:MAX_MESSAGE_LENGTH]
        self.storage.append_event(
            Event(
                task_id=record.task_id,
                event_type=event_type,
                actor_type=(
                    ActorType.HUMAN if event_type is EventType.COMMAND_INVOKED else ActorType.SYSTEM
                ),
                actor_id=(
                    user.username
                    if event_type is EventType.COMMAND_INVOKED
                    else "claude-command-service"
                ),
                metadata=metadata,
            )
        )

    @staticmethod
    def _no_arguments(invocation: ClaudeCommandInvocation) -> None:
        if invocation.parsed.arguments:
            raise CommandArgumentError(f"claude/{invocation.parsed.name} does not accept arguments")

    def _help(self, invocation: ClaudeCommandInvocation) -> CommandResult:
        self._no_arguments(invocation)
        entries = self.metadata(invocation.task.task_id, invocation.user)
        return CommandResult(
            "claude/help",
            "completed",
            f"{len(entries)} audited Claude capabilities.",
            {"commands": [asdict(item) for item in entries]},
        )

    def _status(self, invocation: ClaudeCommandInvocation) -> CommandResult:
        self._no_arguments(invocation)
        data = self.bridge.status(invocation.task)
        return CommandResult(
            "claude/status",
            "completed",
            (
                f"Claude executor: {data['provider']} · "
                f"phase: {data['workflow_phase']} · mode: {data['execution_mode']}."
            ),
            data,
        )
