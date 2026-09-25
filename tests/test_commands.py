"""Demo 2.5 Phase 4 platform commands, using isolated storage and fake services."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_platform.api.app import create_app
from ai_platform.api.presenters import Presenter
from ai_platform.api.routes.stream import event_stream
from ai_platform.auth import AuthenticatedUser, Role
from ai_platform.commands import (
    CommandArgumentError,
    CommandService,
    CommandUnavailableError,
    UnknownCommandError,
    parse_command,
)
from ai_platform.controls import ControlAction, ControlResult, TaskControlService
from ai_platform.events import ActorType, Event, EventType
from ai_platform.models import TaskStatus
from ai_platform.workflow import TransitionMode, WorkflowPhase
from tests.test_messaging import WEB_ACTOR, _client, _context, _waiting_for_human

EXPECTED = [
    "brainstorm",
    "plan",
    "implement",
    "review",
    "human-review",
    "approve",
    "reject",
    "pause",
    "resume",
    "status",
    "tests",
    "help",
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def user(role: Role = Role.DEVELOPER, name: str = "developer") -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=f"id-{name}", username=name, display_name=name.title(), role=role
    )


def service(tmp_path: Path) -> tuple[CommandService, object]:
    context = _context(tmp_path)
    controls = TaskControlService(context.sessions, context.storage, None)
    return CommandService(context.sessions, context.storage, controls), context


class SynchronousRunner:
    def __init__(self, context) -> None:  # noqa: ANN001
        self.context = context

    def run_prepared_turn(self, prepared) -> None:  # noqa: ANN001
        self.context.sessions.run_prepared(prepared)


def test_parser_recognizes_only_leading_slash_and_supports_quotes() -> None:
    assert parse_command("/status").name == "status"
    assert parse_command("  /reject 'Fix the edge case'  ").arguments == ("Fix the edge case",)
    assert parse_command("Please inspect /api/orders") is None


def test_registry_is_complete_ordered_and_rejects_duplicates(tmp_path: Path) -> None:
    commands, _context = service(tmp_path)
    definitions = commands.registry.list()

    assert [item.name for item in definitions] == EXPECTED
    assert all(item.description and item.usage.startswith("/") for item in definitions)
    with pytest.raises(ValueError, match="Duplicate command"):
        commands.registry.register(definitions[0])


def test_unknown_and_malformed_arguments_are_audited(tmp_path: Path) -> None:
    commands, context = service(tmp_path)

    with pytest.raises(UnknownCommandError, match='Unknown command "/foobar"'):
        commands.execute("DEMO-1", "/foobar do-not-store-this", user(), "unknown-0001")
    with pytest.raises(CommandArgumentError, match="does not accept"):
        commands.execute("DEMO-1", "/status extra", user(), "invalid-0001")

    types = [event.event_type for event in context.storage.get_events("DEMO-1")]
    assert types[-4:] == [
        EventType.COMMAND_INVOKED,
        EventType.COMMAND_FAILED,
        EventType.COMMAND_INVOKED,
        EventType.COMMAND_FAILED,
    ]
    unknown_events = context.storage.get_events("DEMO-1")[-4:-2]
    assert all(event.metadata["arguments"] == "" for event in unknown_events)


def test_help_is_registry_backed_and_viewer_read_commands_are_available(tmp_path: Path) -> None:
    commands, _context = service(tmp_path)
    viewer = user(Role.VIEWER, "viewer")

    metadata = commands.metadata("DEMO-1", viewer)
    result = commands.execute("DEMO-1", "/help approve", viewer, "help-key-0001")

    assert [item.name.removeprefix("/") for item in metadata] == EXPECTED
    assert {item.name for item in metadata if item.available} == {"/status", "/tests", "/help"}
    assert result.data["commands"][0]["name"] == "/approve"
    assert result.data["commands"][0]["available"] is False


def test_status_tests_and_help_do_not_change_task_record(tmp_path: Path) -> None:
    commands, context = service(tmp_path)
    before = context.storage.get_task("DEMO-1")

    status = commands.execute("DEMO-1", "/status", user(), "status-key-01")
    tests = commands.execute("DEMO-1", "/tests", user(), "tests-key-001")
    commands.execute("DEMO-1", "/help", user(), "help-key-0002")

    assert context.storage.get_task("DEMO-1") == before
    assert status.data["workspace_exists"] is False
    assert tests.data == {"verification_status": "NOT_RUN", "result": None}


@pytest.mark.parametrize(
    ("command", "target"),
    [
        ("brainstorm", WorkflowPhase.BRAINSTORM),
        ("plan", WorkflowPhase.PLAN),
        ("implement", WorkflowPhase.IMPLEMENTATION),
        ("review", WorkflowPhase.REVIEW),
        ("human-review", WorkflowPhase.HUMAN_REVIEW),
    ],
)
def test_phase_commands_use_manual_workflow_service(
    tmp_path: Path, command: str, target: WorkflowPhase
) -> None:
    commands, context = service(tmp_path)
    record = context.storage.get_task("DEMO-1")
    if record.workflow_phase is target:
        context.storage.transition_workflow_phase(
            "DEMO-1",
            target,
            WorkflowPhase.PLAN if target is not WorkflowPhase.PLAN else WorkflowPhase.REVIEW,
            Event(
                task_id="DEMO-1",
                event_type=EventType.WORKFLOW_PHASE_CHANGED,
                actor_type=ActorType.SYSTEM,
                actor_id="test-setup",
                metadata={"from_phase": target.value, "to_phase": "PLAN"},
            ),
        )
    authenticated = user(name="phase-author")

    commands.execute("DEMO-1", f"/{command} 'manual reason'", authenticated, f"phase-{command}")

    transition = [
        event
        for event in context.storage.get_events("DEMO-1")
        if event.event_type is EventType.WORKFLOW_PHASE_CHANGED
    ][-1]
    assert context.storage.get_task("DEMO-1").workflow_phase is target
    assert transition.actor_id == authenticated.username
    assert transition.metadata["transition_mode"] == TransitionMode.MANUAL.value
    assert transition.metadata["reason"] == "manual reason"


def test_delegation_calls_existing_phase_and_control_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands, _context = service(tmp_path)
    phase_calls: list[tuple] = []
    pause_calls: list[tuple] = []

    monkeypatch.setattr(
        commands.workflow,
        "transition",
        lambda *args, **kwargs: phase_calls.append((args, kwargs)) or WorkflowPhase.PLAN,
    )
    monkeypatch.setattr(
        commands.controls,
        "pause",
        lambda task_id, actor: (
            pause_calls.append((task_id, actor))
            or ControlResult(ControlAction.PAUSE, task_id, False, deferred=False)
        ),
    )
    monkeypatch.setattr(
        commands,
        "availability",
        lambda definition, record, actor: type(
            "Availability", (), {"available": True, "disabled_reason": None}
        )(),
    )

    commands.execute("DEMO-1", "/plan", user(), "delegate-001")
    commands.execute("DEMO-1", "/pause", user(), "delegate-002")

    assert len(phase_calls) == 1
    assert phase_calls[0][1]["mode"] is TransitionMode.MANUAL
    assert len(pause_calls) == 1


def test_approve_and_resume_availability_reuses_control_rules(tmp_path: Path) -> None:
    commands, context = service(tmp_path)
    by_name = {item.name: item for item in commands.metadata("DEMO-1", user())}

    assert not by_name["/approve"].available
    assert "human review" in by_name["/approve"].disabled_reason.lower()
    assert not by_name["/resume"].available
    assert by_name["/resume"].disabled_reason == "Task is not paused."
    with pytest.raises(CommandUnavailableError):
        commands.execute("DEMO-1", "/approve", user(), "approve-no-01")
    assert context.storage.get_task("DEMO-1").status is TaskStatus.READY


def test_control_commands_reuse_real_pause_resume_reject_and_approve_paths(
    tmp_path: Path,
) -> None:
    approval_context = _context(tmp_path / "approve")
    _waiting_for_human(approval_context, resting_phase="HUMAN_REVIEW")
    approval_controls = TaskControlService(
        approval_context.sessions, approval_context.storage, None
    )
    approval_commands = CommandService(
        approval_context.sessions, approval_context.storage, approval_controls
    )

    approval_commands.execute("DEMO-1", "/approve", user(), "actual-approve-1")

    assert approval_context.storage.get_task("DEMO-1").status is TaskStatus.COMPLETED
    approved = [
        event
        for event in approval_context.storage.get_events("DEMO-1")
        if event.event_type is EventType.HUMAN_APPROVED
    ]
    assert approved[-1].actor_id == "developer"

    correction_context = _context(tmp_path / "correction")
    _waiting_for_human(correction_context)
    runner = SynchronousRunner(correction_context)
    correction_controls = TaskControlService(
        correction_context.sessions,
        correction_context.storage,
        runner,
    )
    correction_commands = CommandService(
        correction_context.sessions, correction_context.storage, correction_controls
    )
    correction_commands.execute("DEMO-1", "/pause", user(), "actual-pause-01")
    correction_commands.execute("DEMO-1", "/resume Continue carefully", user(), "actual-resume-1")
    correction_commands.execute("DEMO-1", "/reject Fix the null case", user(), "actual-reject-1")

    types = [event.event_type for event in correction_context.storage.get_events("DEMO-1")]
    assert EventType.HUMAN_PAUSED in types
    assert EventType.HUMAN_RESUMED in types
    assert EventType.HUMAN_REJECTED in types


@pytest.mark.anyio
async def test_api_catalog_execution_permissions_and_actor_integrity(tmp_path: Path) -> None:
    context = _context(tmp_path)
    app = create_app(context)
    async with _client(app, username="readonly", role=Role.VIEWER) as client:
        catalog = await client.get("/api/tasks/DEMO-1/commands")
        status = await client.post(
            "/api/tasks/DEMO-1/commands",
            json={"command_text": "/status", "client_command_id": "browser-key-01"},
        )
        forbidden = await client.post(
            "/api/tasks/DEMO-1/commands",
            json={"command_text": "/plan", "client_command_id": "browser-key-02"},
        )
        spoof = await client.post(
            "/api/tasks/DEMO-1/commands",
            json={
                "command_text": "/status",
                "client_command_id": "browser-key-03",
                "actor": "admin",
            },
        )

    assert catalog.status_code == 200
    assert status.status_code == 200 and status.json()["command"] == "/status"
    assert forbidden.status_code == 403
    assert spoof.status_code == 422
    invoked = [
        event
        for event in context.storage.get_events("DEMO-1")
        if event.event_type is EventType.COMMAND_INVOKED
    ]
    assert invoked[-1].actor_id == "readonly"
    assert all(event.actor_id != "admin" for event in invoked)


@pytest.mark.anyio
async def test_end_to_end_history_survives_rebuilt_service_context(tmp_path: Path) -> None:
    context = _context(tmp_path)
    async with _client(create_app(context)) as client:
        assert (await client.get("/api/tasks/DEMO-1/commands")).status_code == 200
        assert (
            await client.post(
                "/api/tasks/DEMO-1/commands",
                json={"command_text": "/status", "client_command_id": "e2e-status-01"},
            )
        ).status_code == 200
        transition = await client.post(
            "/api/tasks/DEMO-1/commands",
            json={"command_text": "/plan", "client_command_id": "e2e-plan-0001"},
        )
        unavailable = await client.post(
            "/api/tasks/DEMO-1/commands",
            json={"command_text": "/approve", "client_command_id": "e2e-approve-1"},
        )

    assert transition.status_code == 200
    assert unavailable.status_code == 409
    assert context.storage.get_task("DEMO-1").workflow_phase is WorkflowPhase.PLAN
    rebuilt = _context(tmp_path)
    events = rebuilt.storage.get_events("DEMO-1")
    assert any(event.event_type is EventType.COMMAND_SUCCEEDED for event in events)
    assert any(event.event_type is EventType.COMMAND_FAILED for event in events)
    phase = [event for event in events if event.event_type is EventType.WORKFLOW_PHASE_CHANGED][-1]
    assert phase.metadata["transition_mode"] == "MANUAL"
    assert phase.actor_id == WEB_ACTOR


@pytest.mark.anyio
async def test_existing_sse_flow_exposes_durable_command_events(tmp_path: Path) -> None:
    commands, context = service(tmp_path)
    cursor = context.storage.get_events("DEMO-1")[-1].sequence_id
    commands.execute("DEMO-1", "/status", user(), "sse-status-001")
    stream = event_stream(
        context,
        Presenter(context.settings),
        "DEMO-1",
        cursor,
        is_disconnected=lambda: _async_bool(False),
        still_authenticated=lambda: _async_bool(True),
        poll_seconds=0.01,
        keepalive_seconds=10,
    )
    try:
        assert (await anext(stream)).startswith("retry:")
        frames = [await anext(stream), await anext(stream)]
    finally:
        await stream.aclose()

    payloads = [json.loads(frame.split("data: ", 1)[1]) for frame in frames]
    assert [payload["event_type"] for payload in payloads] == [
        "COMMAND_INVOKED",
        "COMMAND_SUCCEEDED",
    ]


async def _async_bool(value: bool) -> bool:
    return value


def test_simultaneous_identical_phase_commands_allow_one_mutation(tmp_path: Path) -> None:
    commands, context = service(tmp_path)

    def move(key: str) -> str:
        try:
            commands.execute("DEMO-1", "/plan", user(name=key), f"race-{key}-0001")
            return "ok"
        except Exception as error:  # the losing request must be a safe domain failure
            return type(error).__name__

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(move, ["one", "two"]))

    assert outcomes.count("ok") == 1
    assert context.storage.get_task("DEMO-1").workflow_phase is WorkflowPhase.PLAN
    transitions = [
        event
        for event in context.storage.get_events("DEMO-1")
        if event.event_type is EventType.WORKFLOW_PHASE_CHANGED
    ]
    assert len(transitions) == 1
