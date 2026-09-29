"""Demo 2 Phase 2: durable browser messaging, the turn runner, SSE, and redaction.

Every test uses isolated storage/workspaces and fake executors; none calls Claude.
"""

import dataclasses
import json
import queue
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn

from ai_platform.api.app import create_app
from ai_platform.application import ApplicationContext, create_application_context
from ai_platform.approval import ApprovalError
from ai_platform.auth import Role
from ai_platform.config import Settings
from ai_platform.events import ActorType, Event, EventType
from ai_platform.executors.base import ExecutionRequest, ExecutionResult
from ai_platform.identity import HumanIdentity
from ai_platform.models import LogicalModel, MessageStatus, TaskStatus
from ai_platform.runner import TaskTurnRunner
from ai_platform.workflow import WorkflowPhase
from tests.fakes import FakeAgentExecutor

WEB_ACTOR = "web-tester"


class GatedExecutor(FakeAgentExecutor):
    """Fake executor whose turns can be held open, recording writer concurrency.

    Phase 3: one turn can call the executor more than once (e.g. Implementation
    then Review), all under the same ``execution_id``. Gating still holds open
    only the first call of a gated turn -- concurrency tests only care about a
    turn being "in flight" -- and lets the rest of that turn's calls through
    once released, so a single ``permits.release()`` still unblocks a whole
    turn as these tests expect.
    """

    def __init__(self) -> None:
        super().__init__(fix_on_attempt=1)
        self.gated = False
        self.permits = threading.Semaphore(0)
        self.started: queue.Queue[ExecutionRequest] = queue.Queue()
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        self._gated_execution_ids: set[str] = set()

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.started.put(request)
            with self._lock:
                first_call_of_turn = request.execution_id not in self._gated_execution_ids
                self._gated_execution_ids.add(request.execution_id)
            if self.gated and first_call_of_turn and not self.permits.acquire(timeout=30):
                raise RuntimeError("test never released the gated executor")
            return super().execute(request)
        finally:
            with self._lock:
                self.active -= 1


class ExplodingExecutor(FakeAgentExecutor):
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        raise RuntimeError(f"boom in {request.workspace_path}")


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    root = Path(__file__).parents[1]
    settings = Settings(
        project_root=root,
        tasks_path=root / "tests" / "fixtures" / "demo_tasks.json",
        demo_repository=root / "tests" / "fixtures" / "demo_repo",
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
        verification_timeout_seconds=30,
        max_attempts_per_tier=1,
        lock_heartbeat_seconds=1,
        lock_stale_seconds=10,
    )
    return dataclasses.replace(settings, **overrides)


def _context(
    tmp_path: Path,
    executor: FakeAgentExecutor | None = None,
    **overrides: object,
) -> ApplicationContext:
    chosen = executor or FakeAgentExecutor()
    return create_application_context(
        _settings(tmp_path, **overrides), executor_factory=lambda: chosen
    )


def _waiting_for_human(
    context: ApplicationContext,
    task_id: str = "DEMO-1",
    *,
    resting_phase: str = "IMPLEMENTATION",
) -> None:
    """Drive a task to WAITING_FOR_HUMAN through the real core with the fake executor.

    Phase 3: a clean Implementation now flows straight through Review to Human
    Review in one turn. The default here instead parks the task at
    Implementation's own gate stop (the executor never "fixes" the bug),
    matching the resting state most of these tests exercise: WAITING_FOR_HUMAN
    with a plain message or resume still starting a fresh turn. A concrete
    (non-AUTO) Implementation model override means AUTO escalation never
    applies, so each never-fixing turn -- the setup here and every later
    message/resume turn -- takes exactly one Implementation attempt, exactly
    as these tests expect. Pass ``resting_phase="HUMAN_REVIEW"`` for tests that
    specifically exercise Approve/Reject, where only Human Review is correct.
    """

    executor = context.sessions.executor_factory()
    if resting_phase == "IMPLEMENTATION" and isinstance(executor, FakeAgentExecutor):
        executor.fix_on_attempt = None
        context.storage.set_phase_model_preference(
            task_id,
            WorkflowPhase.IMPLEMENTATION,
            LogicalModel.CLAUDE_SONNET,
            "test-setup",
            "test-setup",
        )
    context.sessions.start(task_id, HumanIdentity(actor_id="cli-user", display_name="cli-user"))
    assert context.storage.get_task(task_id).status is TaskStatus.WAITING_FOR_HUMAN


def provision(app, username: str = WEB_ACTOR, role: Role = Role.DEVELOPER) -> str:
    """Create the user if needed and return a fresh login token (real AuthService)."""

    auth = app.state.auth
    if username not in {user.username for user in auth.list_users()}:
        auth.add_user(username, username.title(), role)
    return auth.create_token(username).secret


@asynccontextmanager
async def _client(
    app, username: str = WEB_ACTOR, role: Role = Role.DEVELOPER
) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client logged in through POST /api/auth/login (session cookie)."""

    token = provision(app, username, role)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/api/auth/login", json={"username": username, "token": token})
        assert login.status_code == 200, login.text
        yield client


def _web_human() -> HumanIdentity:
    return HumanIdentity(actor_id=WEB_ACTOR, display_name=WEB_ACTOR.title())


def _post(client: httpx.AsyncClient, task_id: str, message: str, key: str, **extra: object):
    return client.post(
        f"/api/tasks/{task_id}/messages",
        json={"message": message, "client_message_id": key, **extra},
    )


def _events(context: ApplicationContext, task_id: str, event_type: EventType) -> list[Event]:
    return [e for e in context.storage.get_events(task_id) if e.event_type is event_type]


def _statuses(context: ApplicationContext, task_id: str = "DEMO-1") -> list[MessageStatus]:
    return [message.status for message in context.storage.list_queued_messages(task_id)]


def _wait_for(predicate: Callable[[], bool], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not reached in time")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------- conversation reads


@pytest.mark.anyio
async def test_get_messages_reconstructs_legacy_conversation(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context.storage.append_event(
        Event(
            task_id="DEMO-1",
            event_type=EventType.HUMAN_MESSAGE,
            actor_type=ActorType.HUMAN,
            actor_id="alex",
            metadata={"display_name": "alex", "message": "Check other spellings."},
        )
    )
    context.storage.append_event(
        Event(
            task_id="DEMO-1",
            event_type=EventType.AGENT_MESSAGE,
            actor_type=ActorType.AGENT,
            actor_id="claude",
            metadata={"execution_id": "exec-1", "message": "Only one occurrence.", "attempt": 1},
        )
    )

    async with _client(create_app(context)) as client:
        body = (await client.get("/api/tasks/DEMO-1/messages")).json()

    assert [(m["role"], m["actor_id"], m["content"]) for m in body] == [
        ("human", "alex", "Check other spellings."),
        ("agent", "claude", "Only one occurrence."),
    ]
    assert body[0]["status"] is None and body[0]["channel"] is None
    assert body[1]["turn_id"] == "exec-1"
    assert body[0]["sequence_id"] < body[1]["sequence_id"]


# ---------------------------------------------------------------- submission


@pytest.mark.anyio
async def test_post_accepts_persists_first_and_runner_disabled_does_not_execute(
    tmp_path: Path,
) -> None:
    executor = FakeAgentExecutor()
    context = _context(tmp_path, executor)
    _waiting_for_human(context)
    turns_before = len(executor.requests)

    async with _client(create_app(context)) as client:
        response = await _post(client, "DEMO-1", "  Also check the docs.  ", "key-accept-1")
        messages = (await client.get("/api/tasks/DEMO-1/messages")).json()

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["message_status"] == "QUEUED"
    assert body["duplicate"] is False
    human = _events(context, "DEMO-1", EventType.HUMAN_MESSAGE)[-1]
    assert human.actor_id == WEB_ACTOR
    assert human.metadata["message"] == "Also check the docs."
    assert human.metadata["channel"] == "web"
    assert messages[-1]["id"] == body["message_id"]
    assert messages[-1]["status"] == "QUEUED"
    # Runner disabled: accepted work stays queued and no agent turn ran.
    assert len(executor.requests) == turns_before
    assert _statuses(context) == [MessageStatus.QUEUED]


@pytest.mark.anyio
async def test_browser_cannot_choose_actor(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _waiting_for_human(context)
    before = len(context.storage.get_events("DEMO-1"))

    async with _client(create_app(context)) as client:
        spoofed = await _post(client, "DEMO-1", "hi", "key-spoof-01", actor_id="admin")

    assert spoofed.status_code == 422
    assert len(context.storage.get_events("DEMO-1")) == before


@pytest.mark.anyio
async def test_submission_errors_are_intentional(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _waiting_for_human(context)

    async with _client(create_app(context)) as client:
        unknown = await _post(client, "NOPE-9", "hi", "key-unknown")
        blank = await _post(client, "DEMO-1", "   \n ", "key-blank-1")
        oversized = await _post(client, "DEMO-1", "x" * 8001, "key-oversized")
        malformed = await client.post("/api/tasks/DEMO-1/messages", json={"message": "hi"})
        bad_key = await _post(client, "DEMO-1", "hi", "bad key!")
        not_started = await _post(client, "DEMO-2", "hi", "key-ready-01")

    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "Unknown task ID: NOPE-9"}
    assert blank.status_code == 422
    assert oversized.status_code == 422
    assert malformed.status_code == 422
    assert bad_key.status_code == 422
    assert not_started.status_code == 409
    assert not_started.json()["detail"].startswith(
        "This task cannot receive messages in its current state"
    )
    assert "Traceback" not in not_started.text
    assert _events(context, "DEMO-2", EventType.HUMAN_MESSAGE) == []


@pytest.mark.anyio
async def test_viewer_cannot_send_messages(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _waiting_for_human(context)

    async with _client(create_app(context), "watcher", Role.VIEWER) as client:
        response = await _post(client, "DEMO-1", "hi", "key-disabled")
        detail = (await client.get("/api/tasks/DEMO-1")).json()
        messages = await client.get("/api/tasks/DEMO-1/messages")

    assert response.status_code == 403
    assert "Read-only" in response.json()["detail"]
    assert detail["messaging"]["accepting"] is False
    assert messages.status_code == 200  # viewers can read
    assert _events(context, "DEMO-1", EventType.HUMAN_MESSAGE) == []


# ---------------------------------------------------------------- idempotency


@pytest.mark.anyio
async def test_duplicate_client_message_id_is_idempotent(tmp_path: Path) -> None:
    executor = FakeAgentExecutor()
    context = _context(tmp_path, executor)
    _waiting_for_human(context)
    runner = TaskTurnRunner(context.sessions, context.storage)
    turns_before = len(executor.requests)

    async with _client(create_app(context, runner=runner)) as client:
        first = await _post(client, "DEMO-1", "Please double-check.", "retry-key-abc")
        retry = await _post(client, "DEMO-1", "Please double-check.", "retry-key-abc")
        runner.run_pending()
        after_run = await _post(client, "DEMO-1", "Please double-check.", "retry-key-abc")
        conflict = await _post(client, "DEMO-1", "Different text", "retry-key-abc")

    assert first.status_code == retry.status_code == after_run.status_code == 202
    assert retry.json()["duplicate"] is True
    assert first.json()["message_id"] == retry.json()["message_id"]
    assert after_run.json()["message_status"] == "COMPLETED"
    assert conflict.status_code == 409
    assert len(_events(context, "DEMO-1", EventType.HUMAN_MESSAGE)) == 1
    assert len(context.storage.list_queued_messages("DEMO-1")) == 1
    assert len(executor.requests) == turns_before + 1


# ---------------------------------------------------------------- concurrency


@pytest.mark.anyio
async def test_one_writer_per_task_and_fifo_queue(tmp_path: Path) -> None:
    executor = GatedExecutor()
    context = _context(tmp_path, executor)
    _waiting_for_human(context)
    while not executor.started.empty():
        executor.started.get_nowait()
    executor.gated = True
    runner = TaskTurnRunner(context.sessions, context.storage, poll_interval_seconds=0.05)
    runner.start()
    try:
        async with _client(create_app(context, runner=runner)) as client:
            posted_a = time.monotonic()
            response_a = await _post(client, "DEMO-1", "Message A", "key-message-a")
            assert time.monotonic() - posted_a < 2.0
            request_a = executor.started.get(timeout=30)

            posted_b = time.monotonic()
            response_b = await _post(client, "DEMO-1", "Message B", "key-message-b")
            response_c = await _post(client, "DEMO-1", "Message C", "key-message-c")
            assert time.monotonic() - posted_b < 2.0  # never waits for the blocked agent
            assert [r.status_code for r in (response_a, response_b, response_c)] == [202] * 3

            detail = (await client.get("/api/tasks/DEMO-1")).json()
            assert detail["agent_working"] is True
            assert detail["writer"] == "claude"
            assert _statuses(context) == [
                MessageStatus.RUNNING,
                MessageStatus.QUEUED,
                MessageStatus.QUEUED,
            ]
            time.sleep(0.3)  # several dispatcher ticks: still exactly one writer
            assert executor.active == 1

            executor.permits.release()  # finish A
            request_b = executor.started.get(timeout=30)
            _wait_for(lambda: _statuses(context)[0] is MessageStatus.COMPLETED)
            assert _statuses(context)[1:] == [MessageStatus.RUNNING, MessageStatus.QUEUED]

            executor.permits.release()  # finish B
            request_c = executor.started.get(timeout=30)
            executor.permits.release()  # finish C
            _wait_for(lambda: _statuses(context) == [MessageStatus.COMPLETED] * 3)
    finally:
        executor.gated = False
        for _ in range(3):
            executor.permits.release()
        runner.stop()

    assert executor.max_active == 1
    # FIFO, one turn per message, each turn answering the conversation up to its message.
    assert request_a.human_messages[-1] == f"{WEB_ACTOR}: Message A"
    assert request_b.human_messages[-1] == f"{WEB_ACTOR}: Message B"
    assert request_c.human_messages[-1] == f"{WEB_ACTOR}: Message C"
    assert all(request.continuation for request in (request_a, request_b, request_c))
    assert all(
        request.workspace_path == context.workspaces.get_path("DEMO-1")
        for request in (request_a, request_b, request_c)
    )
    assert context.storage.get_task("DEMO-1").active_execution is None
    assert context.storage.get_task("DEMO-1").status is TaskStatus.WAITING_FOR_HUMAN


# ---------------------------------------------------------------- durability / failure


@pytest.mark.anyio
async def test_queued_message_survives_restart_and_runs_later(tmp_path: Path) -> None:
    first = _context(tmp_path)
    _waiting_for_human(first)
    async with _client(create_app(first)) as client:
        accepted = await _post(client, "DEMO-1", "Queued before restart", "key-restart-1")
    assert accepted.status_code == 202

    executor = FakeAgentExecutor()
    restarted = _context(tmp_path, executor)
    assert _statuses(restarted) == [MessageStatus.QUEUED]
    handled = TaskTurnRunner(restarted.sessions, restarted.storage).run_pending()

    assert handled == 1
    assert _statuses(restarted) == [MessageStatus.COMPLETED]
    assert executor.requests[-1].human_messages[-1] == f"{WEB_ACTOR}: Queued before restart"
    human = _events(restarted, "DEMO-1", EventType.HUMAN_MESSAGE)[-1]
    agent_started = _events(restarted, "DEMO-1", EventType.AGENT_STARTED)[-1]
    assert agent_started.sequence_id > human.sequence_id


@pytest.mark.anyio
async def test_agent_failure_is_persisted_and_history_is_kept(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _waiting_for_human(context)
    async with _client(create_app(context)) as client:
        await _post(client, "DEMO-1", "This turn will crash", "key-failure-1")

    exploding = _context(tmp_path, ExplodingExecutor())
    TaskTurnRunner(exploding.sessions, exploding.storage).run_pending()

    async with _client(create_app(exploding)) as client:
        messages = (await client.get("/api/tasks/DEMO-1/messages")).json()
        detail = (await client.get("/api/tasks/DEMO-1")).json()
        rejected = await _post(client, "DEMO-1", "Another one", "key-failure-2")

    failed = messages[-1]
    assert failed["content"] == "This turn will crash"
    assert failed["status"] == "FAILED"
    assert "FAILED" in failed["error"]
    assert detail["status"] == "FAILED"
    assert _events(exploding, "DEMO-1", EventType.TASK_FAILED)
    assert rejected.status_code == 409
    assert any(m["content"] == "Fake executor completed" for m in messages)  # history kept


def test_queued_message_fails_cleanly_when_task_is_no_longer_continuable(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    _waiting_for_human(context, resting_phase="HUMAN_REVIEW")
    submitted = create_app(context).state.conversation.submit(
        "DEMO-1", "late", "key-late-01", _web_human()
    )
    cli_user = HumanIdentity(actor_id="cli-user", display_name="cli-user")
    # Platform rule: approving would orphan the queued instruction, so it is refused
    # (also from the CLI) ...
    with pytest.raises(ApprovalError, match="still queued"):
        context.sessions.approve("DEMO-1", cli_user)
    # ... but a reset can still take the task out of its continuable state.
    context.sessions.reset("DEMO-1", cli_user)

    TaskTurnRunner(context.sessions, context.storage).run_pending()

    message = context.storage.list_queued_messages("DEMO-1")[0]
    assert message.message_id == submitted.message.message_id
    assert message.status is MessageStatus.FAILED
    assert "READY" in message.error


def test_runner_fails_messages_interrupted_by_a_process_stop(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _waiting_for_human(context)
    create_app(context).state.conversation.submit(
        "DEMO-1", "interrupted", "key-interrupt", _web_human()
    )
    claimed = context.storage.claim_next_message("DEMO-1", "dead-execution")
    assert claimed is not None and claimed.status is MessageStatus.RUNNING

    runner = TaskTurnRunner(context.sessions, context.storage)
    assert runner.fail_interrupted_messages() == []  # a fresh claim may be another process's
    runner.interrupt_grace = timedelta(0)
    failed = runner.fail_interrupted_messages()

    assert failed == [claimed.message_id]
    assert _statuses(context) == [MessageStatus.FAILED]


def test_cli_message_path_is_unchanged_and_shared(tmp_path: Path) -> None:
    executor = FakeAgentExecutor()
    context = _context(tmp_path, executor)
    _waiting_for_human(context)

    outcome = context.sessions.message(
        "DEMO-1", "CLI instruction", HumanIdentity(actor_id="alex", display_name="alex")
    )

    assert outcome.agent_started is True
    assert executor.requests[-1].human_messages[-1] == "alex: CLI instruction"
    assert context.storage.list_queued_messages("DEMO-1") == []  # CLI never uses the queue


# ---------------------------------------------------------------- redaction


@pytest.mark.anyio
async def test_absolute_workspace_paths_are_redacted(tmp_path: Path) -> None:
    context = _context(tmp_path)
    workspace = context.settings.workspace_root / "DEMO-1"
    context.storage.append_event(
        Event(
            task_id="DEMO-1",
            event_type=EventType.TEST_PASSED,
            actor_type=ActorType.SYSTEM,
            actor_id="pytest-verifier",
            metadata={
                "exit_code": 0,
                "stdout": f"rootdir: {workspace}\nconfigfile: {workspace}/pyproject.toml\n"
                "platform linux -- /usr/lib/python3.12\n",
                "command": ["python", "-m", "pytest", "tests/test_messages.py"],
            },
        )
    )
    context.storage.append_event(
        Event(
            task_id="DEMO-1",
            event_type=EventType.AGENT_MESSAGE,
            actor_type=ActorType.AGENT,
            actor_id="claude",
            metadata={"message": f"I edited {workspace}/app/messages.py"},
        )
    )

    async with _client(create_app(context)) as client:
        events_text = (await client.get("/api/tasks/DEMO-1/events")).text
        detail = (await client.get("/api/tasks/DEMO-1")).json()
        messages = (await client.get("/api/tasks/DEMO-1/messages")).json()

    assert str(tmp_path) not in events_text
    assert "rootdir: <task-workspace>" in events_text
    assert "<task-workspace>/pyproject.toml" in events_text
    assert "/usr/lib/python3.12" in events_text  # unrelated paths are kept
    assert detail["verification_result"]["stdout"].startswith("rootdir: <task-workspace>\n")
    assert messages[-1]["content"] == "I edited <task-workspace>/app/messages.py"
    # Persisted history is untouched.
    stored = _events(context, "DEMO-1", EventType.TEST_PASSED)[-1]
    assert str(workspace) in stored.metadata["stdout"]


# ---------------------------------------------------------------- GET stays observational


@pytest.mark.anyio
async def test_new_get_endpoints_do_not_mutate_state(tmp_path: Path) -> None:
    context = _context(tmp_path)
    _waiting_for_human(context)
    create_app(context).state.conversation.submit("DEMO-1", "queued", "key-readonly", _web_human())
    before = (
        context.sessions.list_tasks(),
        context.storage.get_events("DEMO-1"),
        context.storage.list_queued_messages("DEMO-1"),
    )

    async with _client(create_app(context)) as client:
        for path in (
            "/api/config",
            "/api/tasks",
            "/api/tasks/DEMO-1",
            "/api/tasks/DEMO-1/messages",
            "/api/tasks/DEMO-1/events",
            "/api/tasks/DEMO-1/diff",
        ):
            assert (await client.get(path)).status_code == 200

    after = (
        context.sessions.list_tasks(),
        context.storage.get_events("DEMO-1"),
        context.storage.list_queued_messages("DEMO-1"),
    )
    assert after == before


# ---------------------------------------------------------------- SSE


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[tuple[str, ApplicationContext]]:
    """Run the real ASGI app under uvicorn so SSE is exercised over HTTP."""

    context = _context(tmp_path)
    app = create_app(context, stream_poll_seconds=0.05, stream_keepalive_seconds=0.2)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for(lambda: server.started, timeout=10)
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    token = provision(app)
    login = httpx.post(f"{base}/api/auth/login", json={"username": WEB_ACTOR, "token": token})
    assert login.status_code == 200
    SSE_COOKIES.clear()
    SSE_COOKIES.update(login.cookies)
    try:
        yield base, context
    finally:
        server.should_exit = True
        thread.join(timeout=10)


SSE_COOKIES: dict[str, str] = {}


def _read_sse(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    until: Callable[[list[dict]], bool],
) -> tuple[list[dict], str]:
    """Collect parsed SSE frames until ``until`` is satisfied, then disconnect."""

    frames: list[dict] = []
    raw: list[str] = []
    current: dict = {}
    with httpx.stream("GET", url, headers=headers, cookies=SSE_COOKIES, timeout=10) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            raw.append(line)
            if line == "":
                if current:
                    frames.append(current)
                    current = {}
                    if until(frames):
                        break
                continue
            field, _, value = line.partition(":")
            current[field or "comment"] = value.lstrip(" ")
    return frames, "\n".join(raw)


def _platform_events(frames: list[dict]) -> list[dict]:
    return [f for f in frames if f.get("event") == "platform_event"]


def _append(context: ApplicationContext, event_type: EventType, **metadata: object) -> int:
    event = context.storage.append_event(
        Event(
            task_id="DEMO-1",
            event_type=event_type,
            actor_type=ActorType.SYSTEM,
            actor_id="test",
            metadata=metadata,
        )
    )
    return event.sequence_id


def test_sse_streams_ordered_public_events_and_resumes_after_last_event_id(
    live_server: tuple[str, ApplicationContext],
) -> None:
    base, context = live_server
    workspace = context.settings.workspace_root / "DEMO-1"
    first = _append(context, EventType.HUMAN_MESSAGE, message="#200")
    second = _append(
        context,
        EventType.AGENT_STARTED,
        session_id="secret-session",
        previous_owner_token="agent:host:1:secret",
        execution_hostname="vps-host",
        path=str(workspace),
        tier="cheap",
    )
    third = _append(context, EventType.TEST_STARTED, command=["python", "-m", "pytest"])
    url = f"{base}/api/tasks/DEMO-1/stream"

    frames, raw = _read_sse(
        f"{url}?after={first - 1}",
        until=lambda fs: (
            len(_platform_events(fs)) >= 3 and any(f.get("event") == "conversation" for f in fs)
        ),
    )
    streamed = _platform_events(frames)
    assert [int(f["id"]) for f in streamed] == [first, second, third]
    assert [json.loads(f["data"])["event_type"] for f in streamed] == [
        "HUMAN_MESSAGE",
        "AGENT_STARTED",
        "TEST_STARTED",
    ]
    assert json.loads(streamed[1]["data"])["metadata"] == {"tier": "cheap"}
    for secret in ("secret-session", "agent:host:1:secret", "vps-host", str(workspace)):
        assert secret not in raw
    assert any(f.get("event") == "conversation" for f in frames)

    # Client disconnected after `second`; EventSource reconnects with Last-Event-ID.
    fourth = _append(context, EventType.TEST_PASSED, exit_code=0)
    resumed, _raw = _read_sse(
        url,
        headers={"Last-Event-ID": str(second)},
        until=lambda fs: len(_platform_events(fs)) >= 2,
    )
    assert [int(f["id"]) for f in _platform_events(resumed)] == [third, fourth]

    # Live events arrive while connected, and idle streams get heartbeats.
    def append_later() -> None:
        time.sleep(0.4)
        _append(context, EventType.AGENT_MESSAGE, message="live")

    threading.Thread(target=append_later, daemon=True).start()
    live, _raw = _read_sse(
        f"{url}?after={fourth}",
        until=lambda fs: len(_platform_events(fs)) >= 1,
    )
    assert json.loads(_platform_events(live)[0]["data"])["metadata"]["message"] == "live"
    assert any(f.get("event") == "heartbeat" for f in live)


@pytest.mark.anyio
async def test_sse_rejects_unknown_task_and_bad_cursor(tmp_path: Path) -> None:
    async with _client(create_app(_context(tmp_path))) as client:
        unknown = await client.get("/api/tasks/NOPE-1/stream")
        bad = await client.get("/api/tasks/DEMO-1/stream", headers={"Last-Event-ID": "abc"})

    assert unknown.status_code == 404
    assert bad.status_code == 422


def test_runner_and_session_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AI_PLATFORM_ENABLE_RUNNER",
        "AI_PLATFORM_SESSION_HOURS",
        "AI_PLATFORM_COOKIE_SECURE",
        "AI_PLATFORM_ALLOWED_ORIGINS",
    ):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings.from_env(tmp_path)
    assert defaults.enable_runner is False
    assert (defaults.session_hours, defaults.cookie_secure, defaults.allowed_origins) == (
        12,
        False,
        (),
    )

    monkeypatch.setenv("AI_PLATFORM_ENABLE_RUNNER", "yes")
    monkeypatch.setenv("AI_PLATFORM_SESSION_HOURS", "8")
    monkeypatch.setenv("AI_PLATFORM_COOKIE_SECURE", "1")
    monkeypatch.setenv("AI_PLATFORM_ALLOWED_ORIGINS", "https://ai.example.com/")
    configured = Settings.from_env(tmp_path)
    assert configured.enable_runner is True and configured.session_hours == 8
    assert configured.cookie_secure is True
    assert configured.allowed_origins == ("https://ai.example.com",)

    monkeypatch.setenv("AI_PLATFORM_ENABLE_RUNNER", "maybe")
    with pytest.raises(ValueError, match="AI_PLATFORM_ENABLE_RUNNER"):
        Settings.from_env(tmp_path)
    monkeypatch.setenv("AI_PLATFORM_ENABLE_RUNNER", "0")
    monkeypatch.setenv("AI_PLATFORM_ALLOWED_ORIGINS", "*")
    with pytest.raises(ValueError, match="AI_PLATFORM_ALLOWED_ORIGINS"):
        Settings.from_env(tmp_path)
