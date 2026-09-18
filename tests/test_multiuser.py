"""Demo 2 Phase 4: several users, several API processes, one TaskSession.

"Process A" and "process B" are two independently composed applications (own
ApplicationContext, SQLiteStorage, services and runner) sharing one runtime
database, so nothing in-memory is shared between them — as with two uvicorn
processes. No test calls Claude.
"""

import asyncio
import importlib.util
import json
import sqlite3
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from ai_platform.api.app import create_app
from ai_platform.auth import Role
from ai_platform.events import EventType
from ai_platform.identity import HumanIdentity
from ai_platform.models import MessageStatus, TaskStatus
from ai_platform.runner import TaskTurnRunner
from ai_platform.storage import SQLiteStorage
from tests.test_messaging import (
    SSE_COOKIES,
    GatedExecutor,
    _client,
    _context,
    _events,
    _post,
    _read_sse,
    _statuses,
    _wait_for,
    _waiting_for_human,
    live_server,  # noqa: F401  (fixture)
    provision,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Process:
    """One independently composed API 'process' over the shared runtime."""

    def __init__(self, tmp_path: Path, executor: GatedExecutor, *, runner: bool = True) -> None:
        self.context = _context(tmp_path, executor)
        self.runner = (
            TaskTurnRunner(self.context.sessions, self.context.storage, poll_interval_seconds=0.05)
            if runner
            else None
        )
        self.app = create_app(self.context, runner=self.runner)


@pytest.fixture
def two_processes(tmp_path: Path):
    executor = GatedExecutor()
    a = Process(tmp_path, executor)
    b = Process(tmp_path, executor)
    for user in ("vakho", "alex"):
        provision(a.app, user)
    yield a, b, executor
    executor.gated = False
    for _ in range(10):
        executor.permits.release()
    _wait_for(
        lambda: a.context.storage.get_task("DEMO-1").active_execution is None, timeout=60
    )
    for process in (a, b):
        if process.runner is not None:
            process.runner.stop()


def _status(process: Process, task_id: str = "DEMO-1") -> TaskStatus:
    return process.context.storage.get_task(task_id).status


def _writer_free(process: Process, task_id: str = "DEMO-1") -> bool:
    return process.context.storage.get_task(task_id).active_execution is None


# ---------------------------------------------------------------- per-user idempotency


@pytest.mark.anyio
async def test_message_keys_are_scoped_per_user(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _waiting_for_human(context)
    app = create_app(context)
    async with _client(app, "vakho") as vakho, _client(app, "alex") as alex:
        first = await _post(vakho, "DEMO-1", "Vakho's note", "shared-key-01")
        other = await _post(alex, "DEMO-1", "Alex's note", "shared-key-01")
        retry = await _post(vakho, "DEMO-1", "Vakho's note", "shared-key-01")
        messages = (await alex.get("/api/tasks/DEMO-1/messages")).json()

    assert (first.status_code, other.status_code, retry.status_code) == (202, 202, 202)
    assert first.json()["message_id"] != other.json()["message_id"]
    assert other.json()["duplicate"] is False and retry.json()["duplicate"] is True
    humans = [m for m in messages if m["role"] == "human"]
    assert [(m["actor_id"], m["actor_display_name"], m["content"]) for m in humans] == [
        ("vakho", "Vakho", "Vakho's note"),
        ("alex", "Alex", "Alex's note"),
    ]


@pytest.mark.anyio
async def test_control_keys_are_scoped_per_user(two_processes) -> None:
    a, b, executor = two_processes
    a.runner.start()
    executor.gated = True
    async with _client(a.app, "vakho") as vakho, _client(a.app, "alex") as alex:
        started = await vakho.post(
            "/api/tasks/DEMO-1/start", json={"client_action_id": "abc-key-1"}
        )
        executor.started.get(timeout=30)
        alex_same_key = await alex.post(
            "/api/tasks/DEMO-1/start", json={"client_action_id": "abc-key-1"}
        )
        vakho_retry = await vakho.post(
            "/api/tasks/DEMO-1/start", json={"client_action_id": "abc-key-1"}
        )
    executor.permits.release()

    assert started.status_code == 202
    assert vakho_retry.status_code == 202 and vakho_retry.json()["duplicate"] is True
    assert vakho_retry.json()["execution_id"] == started.json()["execution_id"]
    # Not Vakho's retry: a normal state conflict for Alex.
    assert alex_same_key.status_code == 409
    assert alex_same_key.json()["detail"] == "Task cannot be started because it is already running."
    assert len(_events(a.context, "DEMO-1", EventType.TASK_STARTED)) == 1


# ---------------------------------------------------------------- migration


def _phase2_storage_module():
    source = subprocess.run(
        ["git", "show", "5172e4a:src/ai_platform/storage.py"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    spec = importlib.util.spec_from_loader("phase2_storage", loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(source, "phase2_storage.py", "exec"), module.__dict__)  # noqa: S102
    return module


def _snapshot(db_path: Path) -> tuple:
    connection = sqlite3.connect(db_path)
    try:
        tables = {
            name: connection.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }
        schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    finally:
        connection.close()
    return tables, schema


def test_phase2_message_queue_migrates_idempotently(tmp_path: Path) -> None:
    from ai_platform.events import ActorType, Event
    from ai_platform.task_loader import load_tasks

    db = tmp_path / "platform.db"
    phase2 = _phase2_storage_module().SQLiteStorage(db)
    phase2.initialize()
    for task in load_tasks(Path(__file__).parents[1] / "tasks.json"):
        phase2.create_task(task)
    for index, actor in enumerate(("vakho", "vakho")):
        phase2.enqueue_message(
            Event(
                task_id="DEMO-1",
                event_type=EventType.HUMAN_MESSAGE,
                actor_type=ActorType.HUMAN,
                actor_id=actor,
                metadata={"display_name": actor, "message": f"phase 2 message {index}"},
            ),
            message_id=f"m-{index}",
            client_message_id=f"phase2-key-{index}",
            display_name=actor,
        )
    before_tables, _ = _snapshot(db)
    assert "UNIQUE (task_id, client_message_id)" in str(_snapshot(db)[1])

    storage = SQLiteStorage(db)
    storage.initialize()  # first Phase 4 open: migrates
    first_tables, first_schema = _snapshot(db)
    storage.initialize()  # second open: no change
    assert _snapshot(db) == (first_tables, first_schema)

    for key in ("events", "tasks", "message_queue"):
        assert first_tables[key] == before_tables[key]  # every row preserved as-is
    assert "UNIQUE (task_id, actor_id, client_message_id)" in str(first_schema)
    assert {"users", "auth_tokens", "web_sessions", "task_presence"} <= set(first_tables)

    migrated = storage.get_queued_message("DEMO-1", "vakho", "phase2-key-0")
    assert migrated is not None and migrated.status is MessageStatus.QUEUED

    # Data written after migration, then a third open: nothing unexpected changes.
    storage.enqueue_message(
        Event(
            task_id="DEMO-1",
            event_type=EventType.HUMAN_MESSAGE,
            actor_type=ActorType.HUMAN,
            actor_id="alex",
            metadata={"display_name": "Alex", "message": "after migration"},
        ),
        message_id="m-new",
        client_message_id="phase2-key-0",  # same key, different author: allowed now
        display_name="Alex",
    )
    written = _snapshot(db)
    storage.initialize()
    assert _snapshot(db) == written
    assert len(written[0]["message_queue"]) == 3


# ---------------------------------------------------------------- two API processes


@pytest.mark.anyio
async def test_simultaneous_start_through_two_processes_runs_once(two_processes) -> None:
    a, b, executor = two_processes
    a.runner.start()
    b.runner.start()
    executor.gated = True
    async with _client(a.app, "vakho") as via_a, _client(b.app, "alex") as via_b:
        responses = await asyncio.gather(
            *(
                client.post("/api/tasks/DEMO-1/start", json={"client_action_id": f"race-key-{i}"})
                for i in range(3)
                for client in (via_a, via_b)
            )
        )
        executor.started.get(timeout=30)
        time.sleep(0.3)
        executor.permits.release()
    _wait_for(lambda: _writer_free(a))
    assert sorted(response.status_code for response in responses) == [202] + [409] * 5
    assert len(_events(a.context, "DEMO-1", EventType.TASK_STARTED)) == 1
    assert executor.max_active == 1


@pytest.mark.anyio
async def test_simultaneous_resume_through_two_processes_runs_once(two_processes) -> None:
    a, b, executor = two_processes
    _waiting_for_human(a.context)
    a.context.sessions.pause("DEMO-1", HumanIdentity(actor_id="cli", display_name="cli"))
    a.runner.start()
    b.runner.start()
    while not executor.started.empty():
        executor.started.get_nowait()
    executor.gated = True
    async with _client(a.app, "vakho") as via_a, _client(b.app, "alex") as via_b:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/tasks/DEMO-1/resume",
                    json={"client_action_id": f"resume-race-{i}", "message": f"note {i}"},
                )
                for i in range(2)
                for client in (via_a, via_b)
            )
        )
        executor.started.get(timeout=30)
        executor.permits.release()
    _wait_for(lambda: _writer_free(a))
    assert sorted(response.status_code for response in responses) == [202, 409, 409, 409]
    # Intent events come only from the lock winner: no duplicated resume/message.
    assert len(_events(a.context, "DEMO-1", EventType.HUMAN_RESUMED)) == 1
    notes = [
        e for e in _events(a.context, "DEMO-1", EventType.HUMAN_MESSAGE)
        if str(e.metadata.get("message", "")).startswith("note ")
    ]
    assert len(notes) == 1
    assert executor.max_active == 1


@pytest.mark.anyio
async def test_same_user_concurrent_duplicate_across_processes_is_a_retry(two_processes) -> None:
    a, b, executor = two_processes
    a.runner.start()
    b.runner.start()
    executor.gated = True
    async with _client(a.app, "vakho") as via_a, _client(b.app, "vakho") as via_b:
        responses = await asyncio.gather(
            via_a.post("/api/tasks/DEMO-1/start", json={"client_action_id": "same-intent-1"}),
            via_b.post("/api/tasks/DEMO-1/start", json={"client_action_id": "same-intent-1"}),
        )
        executor.started.get(timeout=30)
        executor.permits.release()
    assert [response.status_code for response in responses] == [202, 202]
    assert {response.json()["execution_id"] for response in responses}.__len__() == 1
    assert sorted(response.json()["duplicate"] for response in responses) == [False, True]


@pytest.mark.anyio
async def test_messages_through_two_processes_and_two_runners(two_processes) -> None:
    a, b, executor = two_processes
    _waiting_for_human(a.context)
    while not executor.started.empty():
        executor.started.get_nowait()
    executor.gated = True
    a.runner.start()
    b.runner.start()
    async with _client(a.app, "vakho") as via_a, _client(b.app, "alex") as via_b:
        await asyncio.gather(
            _post(via_a, "DEMO-1", "from vakho via A", "multi-proc-1"),
            _post(via_b, "DEMO-1", "from alex via B", "multi-proc-1"),
        )
        first = executor.started.get(timeout=30)
        time.sleep(0.4)  # both runners poll: still one writer, one RUNNING claim
        assert _statuses(a.context).count(MessageStatus.RUNNING) == 1
        executor.permits.release()
        second = executor.started.get(timeout=30)
        executor.permits.release()
    _wait_for(lambda: _statuses(a.context) == [MessageStatus.COMPLETED] * 2)

    rows = a.context.storage.list_queued_messages("DEMO-1")
    assert {row.actor_id for row in rows} == {"vakho", "alex"}
    assert len({row.execution_id for row in rows}) == 2  # one claim per row
    assert first.human_messages[-1].split(": ")[0] == rows[0].actor_id  # FIFO
    assert second.human_messages[-1].split(": ")[0] == rows[1].actor_id
    assert executor.max_active == 1


def test_competing_runners_claim_each_row_once(tmp_path: Path) -> None:
    executor = GatedExecutor()
    a, b = Process(tmp_path, executor), Process(tmp_path, executor)
    _waiting_for_human(a.context)
    human = HumanIdentity(actor_id="vakho", display_name="Vakho")
    for index in range(3):
        a.app.state.conversation.submit("DEMO-1", f"m{index}", f"claim-key-{index}", human)
    barrier = threading.Barrier(2)

    def drain(process: Process) -> None:
        barrier.wait()
        process.runner.run_pending()

    threads = [threading.Thread(target=drain, args=(p,)) for p in (a, b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    rows = a.context.storage.list_queued_messages("DEMO-1")
    assert [row.status for row in rows] == [MessageStatus.COMPLETED] * 3
    assert len(executor.requests) == 1 + 3  # the start turn + exactly one turn per row
    assert executor.max_active == 1


def test_fresh_claims_of_another_process_are_not_failed(tmp_path: Path) -> None:
    executor = GatedExecutor()
    a, b = Process(tmp_path, executor), Process(tmp_path, executor)
    _waiting_for_human(a.context)
    human = HumanIdentity(actor_id="vakho", display_name="Vakho")
    a.app.state.conversation.submit("DEMO-1", "claimed by A", "fresh-claim-1", human)
    claimed = a.context.storage.claim_next_message("DEMO-1", "exec-from-a")
    assert claimed is not None
    # B's periodic interrupted-claim sweep must not steal A's in-flight claim.
    assert b.runner.fail_interrupted_messages() == []
    assert _statuses(a.context) == [MessageStatus.RUNNING]


# ---------------------------------------------------------------- collaboration


@pytest.mark.anyio
async def test_presence_is_ephemeral_and_ttl_based(tmp_path: Path) -> None:
    context = _context(tmp_path)
    app = create_app(context)
    events_before = len(context.storage.get_events("DEMO-1"))
    async with _client(app, "vakho") as vakho, _client(app, "alex") as alex:
        await vakho.post("/api/tasks/DEMO-1/presence")
        both = (await alex.post("/api/tasks/DEMO-1/presence")).json()
        read = (await vakho.get("/api/tasks/DEMO-1/presence")).json()
        other_task = (await vakho.get("/api/tasks/DEMO-2/presence")).json()
        with sqlite3.connect(context.settings.db_path) as connection:
            connection.execute(
                "UPDATE task_presence SET last_seen = ? WHERE user_id = "
                "(SELECT user_id FROM users WHERE username = 'vakho')",
                ((datetime.now(UTC) - timedelta(seconds=60)).isoformat(),),
            )
        expired = (await alex.get("/api/tasks/DEMO-1/presence")).json()
        app.state.auth.set_enabled("alex", False)
        disabled = (await vakho.post("/api/tasks/DEMO-1/presence")).json()

    assert [v["username"] for v in both["viewers"]] == ["alex", "vakho"]
    assert read == both
    assert set(both["viewers"][0]) == {"user_id", "username", "display_name"}
    assert other_task["viewers"] == []
    assert [v["username"] for v in expired["viewers"]] == ["alex"]
    assert [v["username"] for v in disabled["viewers"]] == ["vakho"]
    assert len(context.storage.get_events("DEMO-1")) == events_before  # never task history


@pytest.mark.anyio
async def test_actions_are_attributed_per_user_in_trace(two_processes) -> None:
    a, b, executor = two_processes
    a.runner.start()
    provision(a.app, "an")
    async with (
        _client(a.app, "vakho") as vakho,
        _client(b.app, "alex") as alex,
        _client(a.app, "an") as an,
    ):
        await vakho.post("/api/tasks/DEMO-1/start", json={"client_action_id": "trace-start"})
        _wait_for(lambda: _status(a) is TaskStatus.WAITING_FOR_HUMAN)
        _wait_for(lambda: _writer_free(a))
        assert (await alex.post("/api/tasks/DEMO-1/pause")).status_code == 200
        await an.post("/api/tasks/DEMO-1/resume", json={"client_action_id": "trace-resume"})
        _wait_for(lambda: _status(a) is TaskStatus.WAITING_FOR_HUMAN)
        _wait_for(lambda: _writer_free(a))
        approve_vakho, approve_alex = await asyncio.gather(
            vakho.post("/api/tasks/DEMO-1/approve"), alex.post("/api/tasks/DEMO-1/approve")
        )
        events = (await alex.get("/api/tasks/DEMO-1/events")).json()

    assert sorted([approve_vakho.status_code, approve_alex.status_code]) == [200, 409]
    stale = approve_alex if approve_alex.status_code == 409 else approve_vakho
    # Whichever check saw the winner's commit answers: the availability check
    # ("…waiting for human review") or the core's atomic compare-and-set ("…COMPLETED").
    detail = stale.json()["detail"]
    assert "waiting for human review" in detail or "COMPLETED" in detail
    who = {
        e["event_type"]: (e["actor_id"], e["actor_display_name"])
        for e in events
        if e["event_type"]
        in {"TASK_STARTED", "HUMAN_PAUSED", "HUMAN_RESUMED", "HUMAN_APPROVED"}
    }
    winner = "vakho" if approve_vakho.status_code == 200 else "alex"
    assert who == {
        "TASK_STARTED": ("vakho", "Vakho"),
        "HUMAN_PAUSED": ("alex", "Alex"),
        "HUMAN_RESUMED": ("an", "An"),
        "HUMAN_APPROVED": (winner, winner.title()),
    }
    assert len(_events(a.context, "DEMO-1", EventType.HUMAN_APPROVED)) == 1


def test_two_viewers_receive_the_same_events_and_revoked_streams_end(live_server) -> None:  # noqa: F811
    base, context = live_server
    auth = _auth(context)
    auth.add_user("alex", "Alex", Role.DEVELOPER)
    login_b = httpx.post(
        f"{base}/api/auth/login",
        json={"username": "alex", "token": auth.create_token("alex").secret},
    )
    viewers = (dict(SSE_COOKIES), dict(login_b.cookies))

    from ai_platform.events import ActorType, Event

    event = context.storage.append_event(
        Event(
            task_id="DEMO-1",
            event_type=EventType.HUMAN_MESSAGE,
            actor_type=ActorType.HUMAN,
            actor_id="vakho",
            metadata={"display_name": "Vakho", "message": "seen by everyone"},
        )
    )
    url = f"{base}/api/tasks/DEMO-1/stream?after={event.sequence_id - 1}"
    seen = []
    for cookies in viewers:
        SSE_COOKIES.clear()
        SSE_COOKIES.update(cookies)
        frames, _raw = _read_sse(
            url, until=lambda fs: any(f.get("event") == "platform_event" for f in fs)
        )
        seen.append(next(f for f in frames if f.get("event") == "platform_event"))
    assert seen[0]["id"] == seen[1]["id"] == str(event.sequence_id)
    assert json.loads(seen[0]["data"]) == json.loads(seen[1]["data"])

    # Revoke Alex's sessions: new streams are refused.
    auth.revoke_sessions("alex")
    with httpx.stream("GET", url, cookies=viewers[1], timeout=10) as response:
        assert response.status_code == 401
    SSE_COOKIES.clear()
    SSE_COOKIES.update(viewers[0])


def test_open_stream_ends_when_session_is_revoked(live_server) -> None:  # noqa: F811
    base, context = live_server
    auth = _auth(context)
    url = f"{base}/api/tasks/DEMO-1/stream"
    ended = threading.Event()

    def listen() -> None:
        with httpx.stream("GET", url, cookies=dict(SSE_COOKIES), timeout=10) as response:
            for _line in response.iter_lines():
                pass
        ended.set()

    listener = threading.Thread(target=listen, daemon=True)
    listener.start()
    time.sleep(0.5)
    assert not ended.is_set()
    auth.revoke_sessions("web-tester")
    assert ended.wait(timeout=5), "stream kept running after its session was revoked"


def _auth(context):
    from ai_platform.auth import AuthService

    return AuthService(context.storage, session_ttl=timedelta(hours=1))
