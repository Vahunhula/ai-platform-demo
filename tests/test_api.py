"""Read-only API contract tests using the deterministic local platform core."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from ai_platform.api.app import create_app
from ai_platform.application import ApplicationContext, create_application_context
from ai_platform.config import Settings
from ai_platform.events import ActorType, Event, EventType


def _settings(tmp_path: Path) -> Settings:
    root = Path(__file__).parents[1]
    return Settings(
        project_root=root,
        tasks_path=root / "tasks.json",
        demo_repository=root / "demo_repo",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        db_path=tmp_path / "data" / "platform.db",
        checkpoint_db_path=tmp_path / "data" / "checkpoints.db",
        cheap_model="haiku-test",
        default_model="sonnet-test",
        strong_model="opus-test",
        anthropic_api_key=None,
        executor="claude",
        agent_timeout_seconds=20,
        agent_max_turns=3,
        verification_timeout_seconds=20,
        max_attempts_per_tier=2,
        lock_heartbeat_seconds=1,
        lock_stale_seconds=10,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def api_client(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, ApplicationContext]]:
    context = create_application_context(
        _settings(tmp_path),
    )
    transport = httpx.ASGITransport(app=create_app(context))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, context


@pytest.mark.anyio
async def test_health_endpoint(
    api_client: tuple[httpx.AsyncClient, ApplicationContext],
) -> None:
    client, _context = api_client

    assert (await client.get("/api/health")).json() == {"status": "ok"}


@pytest.mark.anyio
async def test_task_listing_and_known_detail(
    api_client: tuple[httpx.AsyncClient, ApplicationContext],
) -> None:
    client, _context = api_client

    listing = await client.get("/api/tasks")
    detail = await client.get("/api/tasks/demo-1")

    assert listing.status_code == 200
    assert [task["id"] for task in listing.json()] == ["DEMO-1", "DEMO-2", "DEMO-3"]
    assert listing.json()[0] == {
        "id": "DEMO-1",
        "title": "Fix welcome typo",
        "difficulty": "LOW",
        "status": "READY",
        "model_tier": None,
        "writer": None,
    }
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == "DEMO-1"
    assert body["description"] == "Fix the typo in the welcome message."
    assert body["acceptance_criteria"][0] == '"Welcome" is spelled correctly'
    assert body["workspace_id"] is None
    assert "workspace_path" not in body
    assert "execution_owner" not in body


@pytest.mark.anyio
async def test_unknown_task_returns_404(
    api_client: tuple[httpx.AsyncClient, ApplicationContext],
) -> None:
    client, _context = api_client

    response = await client.get("/api/tasks/UNKNOWN")

    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown task ID: UNKNOWN"}


@pytest.mark.anyio
async def test_event_history_is_ordered_and_filters_private_metadata(
    api_client: tuple[httpx.AsyncClient, ApplicationContext],
) -> None:
    client, context = api_client
    context.storage.append_event(
        Event(
            task_id="DEMO-1",
            event_type=EventType.HUMAN_MESSAGE,
            actor_type=ActorType.HUMAN,
            actor_id="alex",
            metadata={
                "message": "Please keep the change small.",
                "execution_id": "public-execution",
                "previous_owner_token": "secret-lock-token",
                "path": "/private/runtime/path",
            },
        )
    )

    response = await client.get("/api/tasks/DEMO-1/events")

    assert response.status_code == 200
    events = response.json()
    assert [event["sequence_id"] for event in events] == sorted(
        event["sequence_id"] for event in events
    )
    assert events[-1]["execution_id"] == "public-execution"
    assert events[-1]["metadata"] == {"message": "Please keep the change small."}


@pytest.mark.anyio
async def test_diff_endpoint_uses_task_workspace(
    api_client: tuple[httpx.AsyncClient, ApplicationContext],
) -> None:
    client, context = api_client
    workspace = context.workspaces.create("DEMO-1")
    context.storage.update_workspace_path("DEMO-1", workspace)
    message_file = workspace / "app" / "messages.py"
    message_file.write_text(
        message_file.read_text(encoding="utf-8").replace("Welocme", "Welcome"),
        encoding="utf-8",
    )

    response = await client.get("/api/tasks/DEMO-1/diff")

    assert response.status_code == 200
    assert response.json()["task_id"] == "DEMO-1"
    assert "app/messages.py" in response.json()["diff"]
    assert "Welcome" in response.json()["diff"]


@pytest.mark.anyio
async def test_get_endpoints_do_not_mutate_task_state(
    api_client: tuple[httpx.AsyncClient, ApplicationContext],
) -> None:
    client, context = api_client
    before_records = context.sessions.list_tasks()
    before_events = {
        record.task_id: context.sessions.get_events(record.task_id)
        for record in before_records
    }

    assert (await client.get("/api/tasks")).status_code == 200
    assert (await client.get("/api/tasks/DEMO-1")).status_code == 200
    assert (await client.get("/api/tasks/DEMO-1/events")).status_code == 200
    assert (await client.get("/api/tasks/DEMO-1/diff")).status_code == 200

    assert context.sessions.list_tasks() == before_records
    assert {
        record.task_id: context.sessions.get_events(record.task_id)
        for record in before_records
    } == before_events


@pytest.mark.anyio
async def test_unexpected_failure_returns_generic_500(
    api_client: tuple[httpx.AsyncClient, ApplicationContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, context = api_client

    def broken_diff(_task_id: str) -> str:
        raise RuntimeError("git failed in /secret/runtime/path")

    monkeypatch.setattr(context.sessions, "get_diff", broken_diff)
    transport = httpx.ASGITransport(app=create_app(context), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as quiet_client:
        response = await quiet_client.get("/api/tasks/DEMO-1/diff")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "/secret" not in response.text
    assert (await client.get("/api/health")).status_code == 200


@pytest.mark.anyio
async def test_human_display_name_is_public_metadata(
    api_client: tuple[httpx.AsyncClient, ApplicationContext],
) -> None:
    client, context = api_client
    context.storage.append_event(
        Event(
            task_id="DEMO-2",
            event_type=EventType.HUMAN_CONNECTED,
            actor_type=ActorType.HUMAN,
            actor_id="alex",
            metadata={"display_name": "Alex"},
        )
    )

    events = (await client.get("/api/tasks/DEMO-2/events")).json()

    assert events[-1]["actor_id"] == "alex"
    assert events[-1]["metadata"] == {"display_name": "Alex"}


def test_api_context_does_not_recover_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "ai_platform.sessions.TaskSessionService.recover_stale_locks",
        lambda _self, *, recovered_by: calls.append(recovered_by) or [],
    )

    create_application_context(_settings(tmp_path))
    create_application_context(_settings(tmp_path), lock_recovery_client="cli-startup")

    assert len(calls) == 1
    assert calls[0].startswith("cli-startup@")


def test_api_routes_are_read_only() -> None:
    methods = {
        method
        for route in create_app().routes
        if getattr(route, "path", "").startswith("/api")
        for method in getattr(route, "methods", set())
    }

    assert methods <= {"GET", "HEAD"}
