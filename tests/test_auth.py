"""Demo 2 Phase 4: authentication, sessions, authorization and cross-site protection."""

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from ai_platform.api.app import create_app
from ai_platform.api.security import SESSION_COOKIE
from ai_platform.auth import ACCESS_TOKEN_PREFIX, Role
from ai_platform.cli import app as cli_app
from ai_platform.events import EventType
from tests.test_messaging import (
    WEB_ACTOR,
    _client,
    _context,
    _events,
    _post,
    _waiting_for_human,
    provision,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _raw(client_app) -> httpx.AsyncClient:
    """An unauthenticated client."""

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=client_app), base_url="http://test")


def _db_dump(db_path: Path) -> str:
    connection = sqlite3.connect(db_path)
    try:
        return "\n".join(connection.iterdump())
    finally:
        connection.close()


# ---------------------------------------------------------------- tokens and login


@pytest.mark.anyio
async def test_tokens_and_sessions_are_stored_only_as_hashes(tmp_path: Path) -> None:
    context = _context(tmp_path)
    app = create_app(context)
    token = provision(app, "vakho", Role.DEVELOPER)
    assert token.startswith(ACCESS_TOKEN_PREFIX) and len(token) >= 40

    async with _raw(app) as client:
        login = await client.post("/api/auth/login", json={"username": "vakho", "token": token})
    session_secret = login.cookies[SESSION_COOKIE]

    dump = _db_dump(context.settings.db_path)
    assert token not in dump and token[len(ACCESS_TOKEN_PREFIX) :] not in dump
    assert session_secret not in dump
    assert login.json() == {
        "id": login.json()["id"],
        "username": "vakho",
        "display_name": "Vakho",
        "role": "developer",
        "can_modify_tasks": True,
    }
    cookie = login.headers["set-cookie"].lower()
    for attribute in ("httponly", "samesite=strict", "path=/"):
        assert attribute in cookie
    assert "secure" not in cookie.replace("samesite", "")  # dev default: plain-HTTP tunnel


@pytest.mark.anyio
async def test_secure_cookie_when_configured(tmp_path: Path) -> None:
    app = create_app(_context(tmp_path, cookie_secure=True))
    token = provision(app)
    async with _raw(app) as client:
        login = await client.post("/api/auth/login", json={"username": WEB_ACTOR, "token": token})
    assert "; secure" in login.headers["set-cookie"].lower()


@pytest.mark.anyio
async def test_login_failures_are_generic(tmp_path: Path) -> None:
    app = create_app(_context(tmp_path))
    token = provision(app, "vakho")
    provision(app, "alex")
    app.state.auth.set_enabled("alex", False)
    alex_token = app.state.auth.create_token("alex").secret

    async with _raw(app) as client:
        attempts = [
            await client.post("/api/auth/login", json={"username": "vakho", "token": "ap_wrong"}),
            await client.post("/api/auth/login", json={"username": "nobody", "token": token}),
            await client.post("/api/auth/login", json={"username": "alex", "token": alex_token}),
        ]
        extra = await client.post(
            "/api/auth/login", json={"username": "vakho", "token": token, "role": "admin"}
        )
    assert [attempt.status_code for attempt in attempts] == [401, 401, 401]
    assert {attempt.text for attempt in attempts} == {'{"detail":"Invalid credentials"}'}
    assert all(SESSION_COOKIE not in attempt.cookies for attempt in attempts)
    assert extra.status_code == 422


@pytest.mark.anyio
async def test_me_logout_and_session_end(tmp_path: Path) -> None:
    context = _context(tmp_path)
    app = create_app(context)
    async with _client(app, "vakho") as client:
        me = (await client.get("/api/auth/me")).json()
        logout = await client.post("/api/auth/logout")
        after_me = await client.get("/api/auth/me")
        after_tasks = await client.get("/api/tasks")
    assert me["username"] == "vakho" and me["role"] == "developer"
    assert logout.status_code == 204
    assert (after_me.status_code, after_tasks.status_code) == (401, 401)


@pytest.mark.anyio
async def test_expired_disabled_and_revoked_sessions_are_rejected(tmp_path: Path) -> None:
    context = _context(tmp_path)
    app = create_app(context)
    auth = app.state.auth

    async with _client(app, "expiring") as client:
        with sqlite3.connect(context.settings.db_path) as connection:
            connection.execute(
                "UPDATE web_sessions SET expires_at = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
            )
        assert (await client.get("/api/tasks")).status_code == 401

    async with _client(app, "alex") as client:
        assert (await client.get("/api/tasks")).status_code == 200
        auth.set_enabled("alex", False)
        assert (await client.get("/api/tasks")).status_code == 401
        auth.set_enabled("alex", True)
        assert (await client.get("/api/tasks")).status_code == 401  # must log in again

    token = provision(app, "an")
    async with _raw(app) as client:
        await client.post("/api/auth/login", json={"username": "an", "token": token})
        assert (await client.get("/api/tasks")).status_code == 200
        token_id = next(t[0] for t in auth.list_tokens() if t[1] == "an")
        auth.revoke_token(token_id)
        assert (await client.get("/api/tasks")).status_code == 401
        relogin = await client.post("/api/auth/login", json={"username": "an", "token": token})
        assert relogin.status_code == 401

    async with _client(app, "vakho") as client:
        assert auth.revoke_sessions("vakho") >= 1
        assert (await client.get("/api/tasks")).status_code == 401


# ---------------------------------------------------------------- authorization


@pytest.mark.anyio
async def test_api_requires_authentication_except_health_and_login(tmp_path: Path) -> None:
    app = create_app(_context(tmp_path))
    async with _raw(app) as client:
        health = await client.get("/api/health")
        blocked = [
            await client.get(path)
            for path in (
                "/api/config",
                "/api/auth/me",
                "/api/tasks",
                "/api/tasks/DEMO-1",
                "/api/tasks/DEMO-1/events",
                "/api/tasks/DEMO-1/messages",
                "/api/tasks/DEMO-1/diff",
                "/api/tasks/DEMO-1/presence",
                "/api/tasks/DEMO-1/stream",
            )
        ]
        post = await _post(client, "DEMO-1", "hi", "anon-key-01")
        start = await client.post("/api/tasks/DEMO-1/start", json={"client_action_id": "k" * 10})
    assert health.status_code == 200
    assert [response.status_code for response in blocked] == [401] * len(blocked)
    assert (post.status_code, start.status_code) == (401, 401)


@pytest.mark.anyio
async def test_viewer_reads_but_cannot_mutate(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _waiting_for_human(context)
    app = create_app(context)
    async with _client(app, "watcher", Role.VIEWER) as client:
        reads = [
            await client.get(path)
            for path in ("/api/tasks", "/api/tasks/DEMO-1", "/api/tasks/DEMO-1/events")
        ]
        presence = await client.post("/api/tasks/DEMO-1/presence")
        message = await _post(client, "DEMO-1", "hi", "viewer-key-1")
        pause = await client.post("/api/tasks/DEMO-1/pause")
    assert [response.status_code for response in reads] == [200, 200, 200]
    assert presence.status_code == 200  # viewing is allowed
    assert (message.status_code, pause.status_code) == (403, 403)
    assert context.storage.get_task("DEMO-1").status.value == "waiting_for_human"


@pytest.mark.anyio
async def test_actor_comes_from_session_not_request(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _waiting_for_human(context)
    app = create_app(context)
    async with _client(app, "alex") as client:
        spoof_body = await _post(client, "DEMO-1", "hi", "spoof-key-01", actor_id="vakho")
        spoof_header = await client.post(
            "/api/tasks/DEMO-1/messages",
            json={"message": "from alex", "client_message_id": "spoof-key-02"},
            headers={"X-User": "vakho", "X-Actor": "vakho"},
        )
    assert spoof_body.status_code == 422
    assert spoof_header.status_code == 202
    assert _events(context, "DEMO-1", EventType.HUMAN_MESSAGE)[-1].actor_id == "alex"


# ---------------------------------------------------------------- cross-site protection


@pytest.mark.anyio
async def test_cross_site_mutations_are_refused(tmp_path: Path) -> None:
    context = _context(tmp_path, allowed_origins=("https://ui.example.com",))
    _waiting_for_human(context)
    app = create_app(context)
    body = {"message": "x", "client_message_id": "origin-key-1"}
    async with _client(app, "vakho") as client:
        evil = await client.post(
            "/api/tasks/DEMO-1/messages", json=body, headers={"Origin": "https://evil.example"}
        )
        fetch_site = await client.post(
            "/api/tasks/DEMO-1/pause", headers={"Sec-Fetch-Site": "cross-site"}
        )
        evil_login = await client.post(
            "/api/auth/login",
            json={"username": "vakho", "token": "x"},
            headers={"Origin": "https://evil.example"},
        )
        same = await client.post(
            "/api/tasks/DEMO-1/messages", json=body, headers={"Origin": "http://test"}
        )
        allowed = await client.post(
            "/api/tasks/DEMO-1/messages",
            json={**body, "client_message_id": "origin-key-2"},
            headers={"Origin": "https://ui.example.com"},
        )
        read = await client.get("/api/tasks", headers={"Origin": "https://evil.example"})
    assert (evil.status_code, fetch_site.status_code, evil_login.status_code) == (403, 403, 403)
    assert (same.status_code, allowed.status_code, read.status_code) == (202, 202, 200)
    assert "access-control-allow-origin" not in same.headers  # no CORS at all


@pytest.mark.anyio
async def test_no_secret_reaches_events_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    context = _context(tmp_path)
    _waiting_for_human(context)
    app = create_app(context)
    token = provision(app, "vakho")
    async with _raw(app) as client:
        login = await client.post("/api/auth/login", json={"username": "vakho", "token": token})
        await _post(client, "DEMO-1", "hello", "secret-check-1")
    session = login.cookies[SESSION_COOKIE]
    events = "\n".join(e.model_dump_json() for e in context.storage.get_events("DEMO-1"))
    for secret in (token, session):
        assert secret not in events and secret not in caplog.text


# ---------------------------------------------------------------- CLI


def test_cli_provisions_users_and_prints_token_once(tmp_path: Path) -> None:
    env = {
        "AI_PLATFORM_DATA_DIR": str(tmp_path / "data"),
        "AI_PLATFORM_WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        "AI_PLATFORM_DB_PATH": str(tmp_path / "data" / "platform.db"),
        "AI_PLATFORM_CHECKPOINT_DB_PATH": str(tmp_path / "data" / "checkpoints.db"),
    }
    runner = CliRunner()
    added = runner.invoke(
        cli_app,
        ["users", "add", "--username", "Vakho", "--display-name", "Vakho", "--role", "developer"],
        env=env,
    )
    bad_role = runner.invoke(
        cli_app,
        ["users", "add", "--username", "x", "--display-name", "X", "--role", "root"],
        env=env,
    )
    created = runner.invoke(cli_app, ["auth-token", "create", "vakho"], env=env)
    listed = runner.invoke(cli_app, ["users", "list"], env=env)
    tokens = runner.invoke(cli_app, ["auth-token", "list"], env=env)
    disabled = runner.invoke(cli_app, ["users", "disable", "vakho"], env=env)

    assert added.exit_code == 0 and "vakho" in added.stdout
    assert bad_role.exit_code == 1
    secret = next(line for line in created.stdout.splitlines() if line.startswith("ap_"))
    assert "will not be shown again" in created.stdout
    assert secret not in listed.stdout + tokens.stdout
    assert "active" in tokens.stdout
    assert disabled.exit_code == 0
    assert secret not in _db_dump(tmp_path / "data" / "platform.db")
