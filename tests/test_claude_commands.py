"""Phase 5 allowlisted Claude namespace and audit tests."""

import json
from pathlib import Path

import pytest

from ai_platform.api.app import create_app
from ai_platform.api.presenters import Presenter
from ai_platform.api.routes.stream import event_stream
from ai_platform.auth import AuthenticatedUser, Role
from ai_platform.claude_commands import (
    ClaudeCommandClassification,
    ClaudeCommandRegistry,
    ClaudeCommandService,
    parse_claude_command,
)
from ai_platform.commands import CommandUnavailableError, UnknownCommandError, parse_command
from ai_platform.controls import PermissionDeniedError
from ai_platform.events import EventType
from ai_platform.workflow import WorkflowPhase
from tests.fakes import FakeAgentExecutor
from tests.test_messaging import _client, _context


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _user(role: Role = Role.DEVELOPER) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=role.value,
        username=role.value,
        display_name=role.value.title(),
        role=role,
    )


def _service(tmp_path: Path, executor: FakeAgentExecutor | None = None):  # noqa: ANN202
    context = _context(tmp_path, executor)
    return ClaudeCommandService(context.sessions, context.storage, context.settings), context


def test_namespace_parser_is_exact_and_does_not_capture_normal_text() -> None:
    assert parse_command("/status").name == "status"
    assert parse_claude_command(" claude/help ").name == "help"
    assert parse_claude_command("claude/status").name == "status"
    assert parse_claude_command("Please inspect claude/config.py") is None
    assert parse_command("claude/help") is None


def test_registry_order_metadata_classification_and_duplicates(tmp_path: Path) -> None:
    service, _context = _service(tmp_path)
    definitions = service.registry.list()
    assert [item.name for item in definitions] == [
        "help",
        "status",
        "auth",
        "config",
        "permissions",
        "session-delete",
        "mcp",
    ]
    assert [item.classification for item in definitions[:2]] == [
        ClaudeCommandClassification.ADAPTED,
        ClaudeCommandClassification.ADAPTED,
    ]
    assert all(item.disabled_reason for item in definitions[2:])
    duplicate = ClaudeCommandRegistry()
    duplicate.register(definitions[0])
    with pytest.raises(ValueError, match="Duplicate Claude command"):
        duplicate.register(definitions[0])


def test_help_status_and_failures_are_durable_and_safe(tmp_path: Path) -> None:
    executor = FakeAgentExecutor()
    service, context = _service(tmp_path, executor)
    help_result = service.execute("DEMO-1", "claude/help", _user(), "claude-help-01")
    status = service.execute("DEMO-1", "claude/status", _user(), "claude-status-1")

    assert help_result.data["commands"][0]["command"] == "claude/help"
    assert status.data["provider"] == "claude"
    assert status.data["workflow_phase"] == "IMPLEMENTATION"
    assert status.data["execution_mode"] == "workspace_write"
    assert "authentication" not in status.data
    assert executor.requests == []

    with pytest.raises(UnknownCommandError, match="Unknown Claude command"):
        service.execute("DEMO-1", "claude/foobar '; touch /tmp/pwned'", _user(), "claude-unknown-1")
    with pytest.raises(CommandUnavailableError, match="forbidden"):
        service.execute("DEMO-1", "claude/auth", _user(), "claude-auth-001")
    assert executor.requests == []

    command_events = [
        event
        for event in context.storage.get_events("DEMO-1")
        if event.event_type
        in {EventType.COMMAND_INVOKED, EventType.COMMAND_SUCCEEDED, EventType.COMMAND_FAILED}
    ]
    assert command_events
    assert all(event.metadata["namespace"] == "claude" for event in command_events)
    assert all("; touch" not in event.metadata["arguments"] for event in command_events)
    restarted = _context(tmp_path)
    assert any(
        event.metadata.get("namespace") == "claude"
        for event in restarted.storage.get_events("DEMO-1")
    )


def test_viewer_gets_read_only_adaptations_but_not_developer_capabilities(tmp_path: Path) -> None:
    service, _context = _service(tmp_path)
    metadata = service.metadata("DEMO-1", _user(Role.VIEWER))
    available = {item.command for item in metadata if item.available}
    assert available == {"claude/help", "claude/status"}
    assert all(not item.available for item in metadata if item.command == "claude/config")
    with pytest.raises(PermissionDeniedError, match="Developer access"):
        service.execute("DEMO-1", "claude/config", _user(Role.VIEWER), "viewer-config-1")


def test_status_reflects_platform_phase_safety_without_running_ai(tmp_path: Path) -> None:
    executor = FakeAgentExecutor()
    service, context = _service(tmp_path, executor)
    with context.storage.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE tasks SET workflow_phase = ? WHERE task_id = 'DEMO-1'",
            (WorkflowPhase.PLAN.value,),
        )
    plan = service.execute("DEMO-1", "claude/status", _user(), "phase-plan-001")
    with context.storage.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE tasks SET workflow_phase = ? WHERE task_id = 'DEMO-1'",
            (WorkflowPhase.HUMAN_REVIEW.value,),
        )
    human_review = service.execute("DEMO-1", "claude/status", _user(), "phase-human-01")
    assert plan.data["execution_mode"] == "read_only"
    assert human_review.data["execution_mode"] == "disabled"
    assert executor.requests == []


@pytest.mark.anyio
async def test_claude_command_events_flow_through_existing_sse(tmp_path: Path) -> None:
    service, context = _service(tmp_path)
    cursor = context.storage.get_events("DEMO-1")[-1].sequence_id
    service.execute("DEMO-1", "claude/status", _user(), "claude-sse-001")

    async def connected() -> bool:
        return False

    async def authenticated() -> bool:
        return True

    stream = event_stream(
        context,
        Presenter(context.settings),
        "DEMO-1",
        cursor,
        is_disconnected=connected,
        still_authenticated=authenticated,
        poll_seconds=0.01,
        keepalive_seconds=10,
    )
    try:
        assert (await anext(stream)).startswith("retry:")
        payloads = [
            json.loads((await anext(stream)).split("data: ", 1)[1]),
            json.loads((await anext(stream)).split("data: ", 1)[1]),
        ]
    finally:
        await stream.aclose()
    assert [payload["event_type"] for payload in payloads] == [
        "COMMAND_INVOKED",
        "COMMAND_SUCCEEDED",
    ]
    assert all(payload["metadata"]["namespace"] == "claude" for payload in payloads)


@pytest.mark.anyio
async def test_api_uses_authenticated_actor_and_unknown_is_never_a_message(tmp_path: Path) -> None:
    executor = FakeAgentExecutor()
    context = _context(tmp_path, executor)
    app = create_app(context)
    async with _client(app, username="actual-developer") as client:
        catalog = await client.get("/api/tasks/DEMO-1/claude-commands")
        status = await client.post(
            "/api/tasks/DEMO-1/claude-commands",
            json={"command_text": "claude/status", "client_command_id": "browser-claude-1"},
        )
        spoof = await client.post(
            "/api/tasks/DEMO-1/claude-commands",
            json={
                "command_text": "claude/status",
                "client_command_id": "browser-claude-2",
                "actor": "attacker",
            },
        )
        unknown = await client.post(
            "/api/tasks/DEMO-1/claude-commands",
            json={"command_text": "claude/nope", "client_command_id": "browser-claude-3"},
        )

    assert catalog.status_code == 200
    assert status.status_code == 200
    assert spoof.status_code == 422
    assert unknown.status_code == 400
    assert executor.requests == []
    invoked = [
        event
        for event in context.storage.get_events("DEMO-1")
        if event.event_type is EventType.COMMAND_INVOKED
        and event.metadata.get("namespace") == "claude"
    ]
    assert {event.actor_id for event in invoked} == {"actual-developer"}
    assert not any(
        event.event_type.name == "HUMAN_MESSAGE" and "claude/nope" in str(event.metadata)
        for event in context.storage.get_events("DEMO-1")
    )
