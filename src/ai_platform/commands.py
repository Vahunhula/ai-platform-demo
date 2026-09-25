"""Deterministic platform slash commands shared by HTTP, CLI, and the browser.

Commands are bounded application operations.  They never reach the agent prompt,
execute a shell, or write task state directly; mutating handlers delegate to the
existing workflow and lifecycle services.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ai_platform.auth import AuthenticatedUser
from ai_platform.controls import (
    READ_ONLY,
    ControlAction,
    ControlResult,
    PermissionDeniedError,
    TaskControlService,
)
from ai_platform.events import ActorType, Event, EventType
from ai_platform.models import TaskRecord
from ai_platform.sessions import MAX_MESSAGE_LENGTH, TaskSessionError, TaskSessionService
from ai_platform.storage import SQLiteStorage
from ai_platform.workflow import TransitionMode, WorkflowPhase
from ai_platform.workflow_services import WorkflowError, WorkflowPhaseService


class CommandError(RuntimeError):
    """Client-safe base class for expected command failures."""


class CommandParseError(CommandError):
    """The slash expression is malformed or is not a command."""


class UnknownCommandError(CommandError):
    """The slash name is not in the platform registry."""


class CommandArgumentError(CommandError):
    """Arguments do not match the command's bounded contract."""


class CommandUnavailableError(CommandError):
    """The command exists but is invalid for the current task state."""


class CommandPermission(StrEnum):
    VIEW = "viewer"
    MODIFY = "developer"


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    name: str
    arguments: tuple[str, ...]

    @property
    def argument_text(self) -> str:
        return " ".join(self.arguments)


def parse_command(text: str) -> ParsedCommand | None:
    """Parse a leading slash command with POSIX-style quoting, but no shell expansion."""

    normalized = text.strip()
    if not normalized.startswith("/"):
        return None
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError as error:
        raise CommandParseError(f"Malformed command: {error}") from error
    if not tokens or not tokens[0].startswith("/"):
        return None
    command_token = tokens[0]
    if command_token == "/" or "/" in command_token[1:]:
        raise CommandParseError("Command name must follow '/' and contain no additional '/'")
    name = command_token[1:].lower()
    if not name.replace("-", "").isalnum():
        raise CommandParseError(f"Malformed command name {command_token!r}")
    return ParsedCommand(name=name, arguments=tuple(tokens[1:]))


CommandHandler = Callable[["CommandInvocation"], "CommandResult"]


@dataclass(frozen=True, slots=True)
class CommandDefinition:
    name: str
    description: str
    usage: str
    arguments: tuple[str, ...]
    required_permission: CommandPermission
    mutating: bool
    handler: CommandHandler


@dataclass(frozen=True, slots=True)
class CommandAvailability:
    available: bool
    disabled_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CommandMetadata:
    name: str
    description: str
    usage: str
    arguments: tuple[str, ...]
    required_permission: CommandPermission
    available: bool
    disabled_reason: str | None
    mutating: bool


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    task_id: str
    parsed: ParsedCommand
    user: AuthenticatedUser
    client_command_id: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: str
    status: str
    message: str
    data: dict[str, Any]
    accepted: bool = False


class CommandRegistry:
    """Small authoritative catalog; insertion order is stable and duplicates fail."""

    def __init__(self) -> None:
        self._commands: dict[str, CommandDefinition] = {}

    def register(self, definition: CommandDefinition) -> None:
        if definition.name in self._commands:
            raise ValueError(f"Duplicate command: /{definition.name}")
        self._commands[definition.name] = definition

    def get(self, name: str) -> CommandDefinition | None:
        return self._commands.get(name.lower().removeprefix("/"))

    def resolve(self, name: str) -> CommandDefinition:
        definition = self.get(name)
        if definition is None:
            shown = name if name.startswith("/") else f"/{name}"
            raise UnknownCommandError(f'Unknown command "{shown}"')
        return definition

    def list(self) -> tuple[CommandDefinition, ...]:
        return tuple(self._commands.values())

    def execute(self, invocation: CommandInvocation) -> CommandResult:
        return self.resolve(invocation.parsed.name).handler(invocation)


_PHASE_COMMANDS = {
    "brainstorm": WorkflowPhase.BRAINSTORM,
    "plan": WorkflowPhase.PLAN,
    "implement": WorkflowPhase.IMPLEMENTATION,
    "review": WorkflowPhase.REVIEW,
    "human-review": WorkflowPhase.HUMAN_REVIEW,
}
_CONTROL_COMMANDS = {
    "approve": ControlAction.APPROVE,
    "reject": ControlAction.REJECT,
    "pause": ControlAction.PAUSE,
    "resume": ControlAction.RESUME,
}


class CommandService:
    """Task-aware command orchestration over the existing core services."""

    def __init__(
        self,
        sessions: TaskSessionService,
        storage: SQLiteStorage,
        controls: TaskControlService,
    ) -> None:
        self.sessions = sessions
        self.storage = storage
        self.controls = controls
        self.workflow = WorkflowPhaseService(storage)
        self.registry = CommandRegistry()
        self._register_commands()

    def _register_commands(self) -> None:
        phase_descriptions = {
            "brainstorm": "Move the workflow to Brainstorm.",
            "plan": "Move the workflow to Plan.",
            "implement": "Move the workflow to Implementation.",
            "review": "Move the workflow to Review.",
            "human-review": "Move the workflow to Human Review.",
        }
        for name in _PHASE_COMMANDS:
            self.registry.register(
                CommandDefinition(
                    name,
                    phase_descriptions[name],
                    f"/{name} [reason]",
                    ("reason (optional)",),
                    CommandPermission.MODIFY,
                    True,
                    self._phase,
                )
            )
        self.registry.register(
            CommandDefinition(
                "approve",
                "Approve and complete the reviewed task.",
                "/approve",
                (),
                CommandPermission.MODIFY,
                True,
                self._approve,
            )
        )
        self.registry.register(
            CommandDefinition(
                "reject",
                "Reject with feedback and start a correction turn.",
                "/reject <feedback>",
                ("feedback (required)",),
                CommandPermission.MODIFY,
                True,
                self._reject,
            )
        )
        self.registry.register(
            CommandDefinition(
                "pause",
                "Pause the task cooperatively.",
                "/pause",
                (),
                CommandPermission.MODIFY,
                True,
                self._pause,
            )
        )
        self.registry.register(
            CommandDefinition(
                "resume",
                "Resume from the current workspace.",
                "/resume [instruction]",
                ("instruction (optional)",),
                CommandPermission.MODIFY,
                True,
                self._resume,
            )
        )
        self.registry.register(
            CommandDefinition(
                "status",
                "Show the current task and workflow status.",
                "/status",
                (),
                CommandPermission.VIEW,
                False,
                self._status,
            )
        )
        self.registry.register(
            CommandDefinition(
                "tests",
                "Show the latest platform verification result.",
                "/tests",
                (),
                CommandPermission.VIEW,
                False,
                self._tests,
            )
        )
        self.registry.register(
            CommandDefinition(
                "help",
                "List platform commands or describe one command.",
                "/help [command]",
                ("command (optional)",),
                CommandPermission.VIEW,
                False,
                self._help,
            )
        )

    def metadata(self, task_id: str, user: AuthenticatedUser) -> list[CommandMetadata]:
        record = self.sessions.get_session(task_id).record
        return [self._metadata(definition, record, user) for definition in self.registry.list()]

    def _metadata(
        self, definition: CommandDefinition, record: TaskRecord, user: AuthenticatedUser
    ) -> CommandMetadata:
        availability = self.availability(definition, record, user)
        return CommandMetadata(
            name=f"/{definition.name}",
            description=definition.description,
            usage=definition.usage,
            arguments=definition.arguments,
            required_permission=definition.required_permission,
            available=availability.available,
            disabled_reason=availability.disabled_reason,
            mutating=definition.mutating,
        )

    def availability(
        self, definition: CommandDefinition, record: TaskRecord, user: AuthenticatedUser
    ) -> CommandAvailability:
        if (
            definition.required_permission is CommandPermission.MODIFY
            and not user.role.can_modify_tasks
        ):
            return CommandAvailability(False, READ_ONLY)
        if definition.name in _PHASE_COMMANDS:
            target = _PHASE_COMMANDS[definition.name]
            state = self.workflow.availability(record.workflow_phase, target, user)
            return CommandAvailability(state.allowed, state.reason)
        if definition.name in _CONTROL_COMMANDS:
            state = self.controls.availability(record, user)[_CONTROL_COMMANDS[definition.name]]
            return CommandAvailability(state.allowed, state.reason)
        return CommandAvailability(True)

    def execute(
        self,
        task_id: str,
        command_text: str,
        user: AuthenticatedUser,
        client_command_id: str,
    ) -> CommandResult:
        # Resolve the task first so even failed attempts are tied to a real task.
        record = self.sessions.get_session(task_id).record
        parsed: ParsedCommand | None = None
        command_name = self._attempted_name(command_text)
        try:
            parsed = parse_command(command_text)
            if parsed is None:
                raise CommandParseError("Input is not a platform slash command")
            command_name = parsed.name
            definition = self.registry.resolve(parsed.name)
            self._audit(
                EventType.COMMAND_INVOKED,
                record,
                user,
                command_name,
                parsed.argument_text,
                client_command_id,
                "invoked",
            )
            available = self.availability(definition, record, user)
            if not available.available:
                if not user.role.can_modify_tasks and definition.mutating:
                    raise PermissionDeniedError(available.disabled_reason or READ_ONLY)
                raise CommandUnavailableError(available.disabled_reason or "Command is unavailable")
            result = self.registry.execute(
                CommandInvocation(record.task_id, parsed, user, client_command_id)
            )
        except Exception as error:
            # Unknown/malformed attempts did not yet receive COMMAND_INVOKED.
            known_definition = self.registry.get(command_name)
            safe_arguments = parsed.argument_text if parsed and known_definition else ""
            if parsed is None or known_definition is None:
                self._audit(
                    EventType.COMMAND_INVOKED,
                    record,
                    user,
                    command_name,
                    safe_arguments,
                    client_command_id,
                    "invoked",
                )
            safe_reason = (
                str(error)
                if isinstance(
                    error,
                    (CommandError, TaskSessionError, WorkflowError),
                )
                else "Command failed unexpectedly"
            )
            self._audit(
                EventType.COMMAND_FAILED,
                self.storage.get_task(record.task_id) or record,
                user,
                command_name,
                safe_arguments,
                client_command_id,
                "failed",
                safe_reason,
            )
            raise
        self._audit(
            EventType.COMMAND_SUCCEEDED,
            self.storage.get_task(record.task_id) or record,
            user,
            command_name,
            parsed.argument_text,
            client_command_id,
            "accepted" if result.accepted else "completed",
            result.message,
        )
        return result

    @staticmethod
    def _attempted_name(command_text: str) -> str:
        token = command_text.strip().split(maxsplit=1)[0][:80]
        return token.removeprefix("/") or "invalid"

    def _audit(
        self,
        event_type: EventType,
        record: TaskRecord,
        user: AuthenticatedUser,
        command: str,
        arguments: str,
        client_command_id: str,
        category: str,
        reason: str | None = None,
    ) -> None:
        metadata: dict[str, Any] = {
            "command": f"/{command}",
            "arguments": arguments[:MAX_MESSAGE_LENGTH],
            "client_command_id": client_command_id,
            "display_name": user.display_name,
            "task_status": record.status.value.upper(),
            "workflow_phase": record.workflow_phase.value,
            "result_category": category,
        }
        if reason:
            metadata["command_result"] = reason[:MAX_MESSAGE_LENGTH]
        self.storage.append_event(
            Event(
                task_id=record.task_id,
                event_type=event_type,
                actor_type=(
                    ActorType.HUMAN if event_type is EventType.COMMAND_INVOKED else ActorType.SYSTEM
                ),
                actor_id=(
                    user.username if event_type is EventType.COMMAND_INVOKED else "command-service"
                ),
                metadata=metadata,
            )
        )

    @staticmethod
    def _no_arguments(invocation: CommandInvocation) -> None:
        if invocation.parsed.arguments:
            raise CommandArgumentError(f"/{invocation.parsed.name} does not accept arguments")

    @staticmethod
    def _control_result(result: ControlResult, message: str) -> CommandResult:
        return CommandResult(
            command=f"/{result.action.value}",
            status="accepted" if result.turn_started else "completed",
            message=message,
            data={
                "action": result.action.value,
                "task_id": result.task_id,
                "execution_id": result.execution_id,
                "duplicate": result.duplicate,
                "deferred": result.deferred,
            },
            accepted=result.turn_started,
        )

    def _phase(self, invocation: CommandInvocation) -> CommandResult:
        reason = invocation.parsed.argument_text or None
        if reason and len(reason) > 4000:
            raise CommandArgumentError("Transition reason is too long")
        record = self.sessions.get_session(invocation.task_id).record
        target = _PHASE_COMMANDS[invocation.parsed.name]
        phase = self.workflow.transition(
            invocation.task_id,
            record.workflow_phase,
            target,
            invocation.user,
            reason=reason,
            mode=TransitionMode.MANUAL,
        )
        return CommandResult(
            f"/{invocation.parsed.name}",
            "completed",
            f"Workflow moved to {phase.value}.",
            {
                "from_phase": record.workflow_phase.value,
                "workflow_phase": phase.value,
                "transition_mode": TransitionMode.MANUAL.value,
            },
        )

    def _approve(self, invocation: CommandInvocation) -> CommandResult:
        self._no_arguments(invocation)
        return self._control_result(
            self.controls.approve(invocation.task_id, invocation.user),
            "Task approved and completed.",
        )

    def _reject(self, invocation: CommandInvocation) -> CommandResult:
        feedback = invocation.parsed.argument_text.strip()
        if not feedback:
            raise CommandArgumentError("/reject requires feedback")
        if len(feedback) > MAX_MESSAGE_LENGTH:
            raise CommandArgumentError("Rejection feedback is too long")
        result = self.controls.reject(
            invocation.task_id, invocation.client_command_id, feedback, invocation.user
        )
        return self._control_result(result, "Rejection recorded; correction turn accepted.")

    def _pause(self, invocation: CommandInvocation) -> CommandResult:
        self._no_arguments(invocation)
        result = self.controls.pause(invocation.task_id, invocation.user)
        message = "Pause requested at the next safe point." if result.deferred else "Task paused."
        return self._control_result(result, message)

    def _resume(self, invocation: CommandInvocation) -> CommandResult:
        message = invocation.parsed.argument_text.strip() or None
        if message and len(message) > MAX_MESSAGE_LENGTH:
            raise CommandArgumentError("Resume instruction is too long")
        result = self.controls.resume(
            invocation.task_id, invocation.client_command_id, message, invocation.user
        )
        return self._control_result(result, "Resume accepted; agent turn started.")

    def _status(self, invocation: CommandInvocation) -> CommandResult:
        self._no_arguments(invocation)
        session = self.sessions.get_session(invocation.task_id)
        record = session.record
        evaluations = self.storage.list_checklist_evaluations(record.task_id)
        readiness = evaluations[-1].readiness if evaluations else None
        availability = self.controls.availability(record, invocation.user)
        data = {
            "task_id": record.task_id,
            "status": record.status.value.upper(),
            "workflow_phase": record.workflow_phase.value,
            "model": record.selected_model,
            "readiness": None
            if readiness is None
            else {
                "score": readiness.score,
                "eligible_for_auto_progression": readiness.eligible_for_auto_progression,
            },
            "verification_status": record.verification_status.value.upper(),
            "workspace_exists": self.sessions.workspace_exists(record.task_id),
            "queued_instructions": self.storage.pending_message_count(record.task_id),
            "controls": {
                action.value: {"available": state.allowed, "disabled_reason": state.reason}
                for action, state in availability.items()
            },
        }
        return CommandResult(
            "/status",
            "completed",
            f"Phase: {record.workflow_phase.value} · Status: {record.status.value.upper()}.",
            data,
        )

    def _tests(self, invocation: CommandInvocation) -> CommandResult:
        self._no_arguments(invocation)
        latest = next(
            (
                event
                for event in reversed(self.storage.get_events(invocation.task_id))
                if event.event_type in {EventType.TEST_PASSED, EventType.TEST_FAILED}
            ),
            None,
        )
        if latest is None:
            return CommandResult(
                "/tests",
                "completed",
                "No platform verification result is available yet.",
                {"verification_status": "NOT_RUN", "result": None},
            )
        result = {
            key: latest.metadata.get(key)
            for key in ("exit_code", "duration_seconds", "timed_out", "stdout", "stderr", "error")
            if key in latest.metadata
        }
        status = "PASSED" if latest.event_type is EventType.TEST_PASSED else "FAILED"
        result.update(
            {
                "status": status,
                "sequence_id": latest.sequence_id,
                "timestamp": latest.timestamp.isoformat(),
            }
        )
        return CommandResult(
            "/tests",
            "completed",
            f"Latest platform verification: {status}.",
            {"verification_status": status, "result": result},
        )

    def _help(self, invocation: CommandInvocation) -> CommandResult:
        if len(invocation.parsed.arguments) > 1:
            raise CommandArgumentError("Usage: /help [command]")
        entries = self.metadata(invocation.task_id, invocation.user)
        if invocation.parsed.arguments:
            requested = invocation.parsed.arguments[0].removeprefix("/").lower()
            entries = [item for item in entries if item.name == f"/{requested}"]
            if not entries:
                raise UnknownCommandError(f'Unknown command "/{requested}"')
        data = {
            "commands": [
                {
                    "name": item.name,
                    "description": item.description,
                    "usage": item.usage,
                    "available": item.available,
                    "disabled_reason": item.disabled_reason,
                }
                for item in entries
            ]
        }
        return CommandResult(
            "/help",
            "completed",
            f"{len(entries)} platform command{'s' if len(entries) != 1 else ''}.",
            data,
        )
