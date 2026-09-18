"""Demo 2 Phase 3: browser lifecycle controls through the shared core.

Isolated runtimes and fake executors only; no test calls Claude.
"""

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from ai_platform.api.app import create_app
from ai_platform.application import ApplicationContext
from ai_platform.auth import Role
from ai_platform.events import EventType
from ai_platform.identity import HumanIdentity
from ai_platform.models import ExecutionKind, MessageStatus, TaskStatus
from ai_platform.runner import TaskTurnRunner
from tests.fakes import FakeAgentExecutor
from tests.test_messaging import (
    WEB_ACTOR,
    GatedExecutor,
    _client,
    _context,
    _events,
    _statuses,
    _wait_for,
    _waiting_for_human,
)

KEY = "action-key-0001"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Harness:
    """API + started runner around an isolated context with a gated fake executor."""

    def __init__(self, tmp_path: Path, **overrides: object) -> None:
        self.executor = GatedExecutor()
        self.context: ApplicationContext = _context(tmp_path, self.executor, **overrides)
        self.runner = TaskTurnRunner(
            self.context.sessions, self.context.storage, poll_interval_seconds=0.05
        )
        self.app = create_app(self.context, runner=self.runner)

    def status(self, task_id: str = "DEMO-1") -> TaskStatus:
        return self.context.storage.get_task(task_id).status

    def idle(self, task_id: str = "DEMO-1") -> bool:
        return self.context.storage.get_task(task_id).active_execution is None

    def drain_started(self) -> None:
        while not self.executor.started.empty():
            self.executor.started.get_nowait()


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    h = Harness(tmp_path)
    h.runner.start()
    try:
        yield h
    finally:
        h.executor.gated = False
        for _ in range(10):
            h.executor.permits.release()
        _wait_for(lambda: all(h.idle(t) for t in ("DEMO-1", "DEMO-2", "DEMO-3")), timeout=60)
        h.runner.stop()


async def _act(client: httpx.AsyncClient, action: str, task_id: str = "DEMO-1", **body):
    return await client.post(f"/api/tasks/{task_id}/{action}", json=body or None)


def _allowed(detail: dict) -> set[str]:
    return {name for name, state in detail["actions"].items() if state["allowed"]}


# ---------------------------------------------------------------- start


@pytest.mark.anyio
async def test_start_is_async_uses_core_path_and_is_idempotent(harness: Harness) -> None:
    harness.executor.gated = True
    async with _client(harness.app) as client:
        began = time.monotonic()
        first = await _act(client, "start", client_action_id=KEY)
        assert time.monotonic() - began < 2.0  # never waits for the agent turn
        request = harness.executor.started.get(timeout=30)

        retry = await _act(client, "start", client_action_id=KEY)
        other_click = await _act(client, "start", client_action_id="action-key-0002")
        detail = (await client.get("/api/tasks/DEMO-1")).json()

        harness.executor.permits.release()
        _wait_for(lambda: harness.status() is TaskStatus.WAITING_FOR_HUMAN and harness.idle())
        after_turn_retry = await _act(client, "start", client_action_id=KEY)

    assert first.status_code == 202
    body = first.json()
    assert (body["status"], body["action"], body["duplicate"]) == ("accepted", "start", False)
    assert retry.status_code == 202 and retry.json()["duplicate"] is True
    assert retry.json()["execution_id"] == body["execution_id"]
    assert after_turn_retry.status_code == 202 and after_turn_retry.json()["duplicate"] is True
    assert other_click.status_code == 409
    assert other_click.json()["detail"] == "Task cannot be started because it is already running."
    assert detail["agent_working"] is True and _allowed(detail) == {"pause"}
    # Same core path as `ai-platform start`: initial (non-continuation) turn, same workspace.
    assert request.continuation is False
    assert request.workspace_path == harness.context.workspaces.get_path("DEMO-1")
    started = _events(harness.context, "DEMO-1", EventType.TASK_STARTED)
    assert len(started) == 1 and started[0].actor_id == WEB_ACTOR
    assert started[0].metadata["execution_id"] == body["execution_id"]
    assert harness.executor.max_active == 1 and len(harness.executor.requests) == 1


@pytest.mark.anyio
async def test_parallel_start_clicks_launch_exactly_one_turn(harness: Harness) -> None:
    harness.executor.gated = True
    async with _client(harness.app) as client:
        responses = await _gather(
            [_act(client, "start", client_action_id=f"parallel-key-{i:04d}") for i in range(4)]
        )
        harness.executor.started.get(timeout=30)
        time.sleep(0.3)
        harness.executor.permits.release()

    codes = sorted(response.status_code for response in responses)
    assert codes == [202, 409, 409, 409]
    _wait_for(lambda: harness.idle())
    assert len(_events(harness.context, "DEMO-1", EventType.TASK_STARTED)) == 1
    assert harness.executor.max_active == 1


async def _gather(coroutines):
    return await asyncio.gather(*coroutines)


@pytest.mark.anyio
async def test_start_rejected_outside_ready(harness: Harness) -> None:
    _waiting_for_human(harness.context)
    async with _client(harness.app) as client:
        response = await _act(client, "start", client_action_id=KEY)
    assert response.status_code == 409
    assert "READY" in response.json()["detail"]


@pytest.mark.anyio
async def test_chat_and_start_race_keeps_one_writer(harness: Harness) -> None:
    harness.executor.gated = True
    async with _client(harness.app) as client:
        early_chat = await client.post(
            "/api/tasks/DEMO-1/messages",
            json={"message": "too early", "client_message_id": "chat-early-01"},
        )
        await _act(client, "start", client_action_id=KEY)
        harness.executor.started.get(timeout=30)
        _wait_for(lambda: harness.status() is TaskStatus.IMPLEMENTING)
        chat = await client.post(
            "/api/tasks/DEMO-1/messages",
            json={"message": "while starting", "client_message_id": "chat-during-1"},
        )
        assert _statuses(harness.context) == [MessageStatus.QUEUED]
        harness.executor.permits.release()  # finish the start turn
        follow_up = harness.executor.started.get(timeout=30)
        harness.executor.permits.release()  # finish the chat turn
        _wait_for(lambda: _statuses(harness.context) == [MessageStatus.COMPLETED])

    assert early_chat.status_code == 409  # READY tasks do not take chat messages
    assert chat.status_code == 202
    assert follow_up.continuation is True
    assert follow_up.human_messages[-1] == f"{WEB_ACTOR}: while starting"
    assert harness.executor.max_active == 1


# ---------------------------------------------------------------- pause / resume


@pytest.mark.anyio
async def test_pause_during_active_turn_is_cooperative(harness: Harness) -> None:
    harness.executor.gated = True
    async with _client(harness.app) as client:
        await _act(client, "start", client_action_id=KEY)
        harness.executor.started.get(timeout=30)
        pause = await _act(client, "pause")
        during = (await client.get("/api/tasks/DEMO-1")).json()
        again = await _act(client, "pause")
        harness.executor.permits.release()
        _wait_for(lambda: harness.status() is TaskStatus.PAUSED_BY_HUMAN and harness.idle())
        after = (await client.get("/api/tasks/DEMO-1")).json()

    assert pause.status_code == 200
    assert pause.json()["status"] == "completed" and pause.json()["deferred"] is True
    assert during["pause_requested"] is True and during["agent_working"] is True
    assert during["status"] != "PAUSED_BY_HUMAN"  # not faked: the turn is still running
    assert again.status_code == 409 and "already requested" in again.json()["detail"]
    paused = _events(harness.context, "DEMO-1", EventType.HUMAN_PAUSED)
    assert paused[-1].actor_id == WEB_ACTOR and paused[-1].metadata["deferred"] is True
    assert _allowed(after) == {"resume", "reset"}


@pytest.mark.anyio
async def test_pause_idle_then_resume_with_message_continues_same_workspace(
    harness: Harness,
) -> None:
    _waiting_for_human(harness.context)
    harness.drain_started()
    async with _client(harness.app) as client:
        pause = await _act(client, "pause")
        double_pause = await _act(client, "pause")
        resume = await _act(
            client, "resume", client_action_id=KEY, message="  Keep the change minimal.  "
        )
        request = harness.executor.started.get(timeout=30)
        _wait_for(lambda: harness.status() is TaskStatus.WAITING_FOR_HUMAN and harness.idle())
        retried = await _act(client, "resume", client_action_id=KEY, message="Keep it minimal.")

    assert pause.json()["deferred"] is False and pause.json()["task_status"] == "PAUSED_BY_HUMAN"
    assert double_pause.status_code == 409 and double_pause.json()["detail"] == (
        "Task is already paused."
    )
    assert resume.status_code == 202 and resume.json()["action"] == "resume"
    assert retried.status_code == 202 and retried.json()["duplicate"] is True
    assert request.continuation is True
    assert request.human_messages[-1] == f"{WEB_ACTOR}: Keep the change minimal."
    assert request.workspace_path == harness.context.workspaces.get_path("DEMO-1")
    assert len(_events(harness.context, "DEMO-1", EventType.HUMAN_RESUMED)) == 1
    assert harness.executor.max_active == 1


@pytest.mark.anyio
async def test_parallel_resume_clicks_launch_one_turn(harness: Harness) -> None:
    _waiting_for_human(harness.context)
    harness.context.sessions.pause("DEMO-1", _cli_human())
    harness.drain_started()
    harness.executor.gated = True
    async with _client(harness.app) as client:
        responses = await _gather(
            [_act(client, "resume", client_action_id=f"resume-key-{i:04d}") for i in range(3)]
        )
        harness.executor.started.get(timeout=30)
        harness.executor.permits.release()
    assert sorted(response.status_code for response in responses) == [202, 409, 409]
    _wait_for(lambda: harness.idle())
    assert len(_events(harness.context, "DEMO-1", EventType.HUMAN_RESUMED)) == 1
    assert harness.executor.max_active == 1


# ---------------------------------------------------------------- approve / reject


@pytest.mark.anyio
async def test_approve_matches_cli_semantics(harness: Harness) -> None:
    _waiting_for_human(harness.context)
    async with _client(harness.app) as client:
        approve = await _act(client, "approve")
        again = await _act(client, "approve")
        detail = (await client.get("/api/tasks/DEMO-1")).json()

    assert approve.status_code == 200 and approve.json()["task_status"] == "COMPLETED"
    assert again.status_code == 409
    types = [e.event_type for e in harness.context.storage.get_events("DEMO-1")][-3:]
    assert types == [EventType.HUMAN_APPROVED, EventType.STATUS_CHANGED, EventType.TASK_COMPLETED]
    assert _events(harness.context, "DEMO-1", EventType.HUMAN_APPROVED)[-1].actor_id == WEB_ACTOR
    assert harness.context.workspaces.exists("DEMO-1")  # retained; nothing pushed
    assert _allowed(detail) == {"reset"}


@pytest.mark.anyio
async def test_approve_refused_during_active_turn_and_with_pending_chat(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)  # runner not started: queued chat stays queued
    _waiting_for_human(h.context)
    async with _client(h.app) as client:
        await client.post(
            "/api/tasks/DEMO-1/messages",
            json={"message": "one more thing", "client_message_id": "pending-chat-1"},
        )
        pending = await _act(client, "approve")

    h2 = Harness(tmp_path / "second")
    h2.executor.gated = True
    h2.runner.start()
    try:
        async with _client(h2.app) as client:
            await _act(client, "start", "DEMO-1", client_action_id=KEY)
            h2.executor.started.get(timeout=30)
            during_turn = await _act(client, "approve")
            h2.executor.permits.release()
            _wait_for(lambda: h2.status() is TaskStatus.WAITING_FOR_HUMAN and h2.idle())
    finally:
        h2.executor.gated = False
        h2.runner.stop()

    assert pending.status_code == 409 and "still queued" in pending.json()["detail"]
    assert h.status() is TaskStatus.WAITING_FOR_HUMAN
    assert during_turn.status_code == 409
    assert during_turn.json()["detail"] == (
        "Task cannot be approved until it is waiting for human review."
    )


@pytest.mark.anyio
async def test_reject_records_feedback_and_runs_a_correction_turn(harness: Harness) -> None:
    _waiting_for_human(harness.context)
    harness.drain_started()
    async with _client(harness.app) as client:
        blank = await _act(client, "reject", client_action_id=KEY, message="   ")
        reject = await _act(client, "reject", client_action_id=KEY, message="Use a constant.")
        request = harness.executor.started.get(timeout=30)
        _wait_for(lambda: harness.status() is TaskStatus.WAITING_FOR_HUMAN and harness.idle())
        invalid = await _act(client, "reject", "DEMO-2", client_action_id=KEY, message="x")

    assert blank.status_code == 422
    assert reject.status_code == 202 and reject.json()["action"] == "reject"
    rejected = _events(harness.context, "DEMO-1", EventType.HUMAN_REJECTED)[-1]
    assert rejected.actor_id == WEB_ACTOR and rejected.metadata["message"] == "Use a constant."
    assert request.continuation is True
    assert request.human_messages[-1] == f"{WEB_ACTOR}: Use a constant."
    assert harness.status() is TaskStatus.WAITING_FOR_HUMAN  # reject is not failure
    assert invalid.status_code == 409
    assert "waiting for human review" in invalid.json()["detail"]


# ---------------------------------------------------------------- reset


@pytest.mark.anyio
async def test_reset_requires_confirmation_and_matches_cli(harness: Harness) -> None:
    _waiting_for_human(harness.context)
    events_before = len(harness.context.storage.get_events("DEMO-1"))
    async with _client(harness.app) as client:
        missing = await client.post("/api/tasks/DEMO-1/reset", json={})
        unconfirmed = await _act(client, "reset", confirm=False)
        forced = await _act(client, "reset", confirm=True, force=True)
        reset = await _act(client, "reset", confirm=True)
        again = await _act(client, "reset", confirm=True)
        public = (await client.get("/api/tasks/DEMO-1/events")).json()
        detail = (await client.get("/api/tasks/DEMO-1")).json()

    assert [missing.status_code, unconfirmed.status_code, forced.status_code] == [422] * 3
    assert reset.status_code == 200 and reset.json()["task_status"] == "READY"
    assert again.status_code == 409  # already in the initial READY state
    assert not harness.context.workspaces.exists("DEMO-1")
    record = harness.context.storage.get_task("DEMO-1")
    assert (record.selected_tier, record.attempt, record.workspace_path) == (None, 0, None)
    events = harness.context.storage.get_events("DEMO-1")
    assert len(events) == events_before + 2  # history retained, two events appended
    assert [e.event_type for e in events[-2:]] == [EventType.WORKSPACE_RESET, EventType.TASK_RESET]
    assert "deleted_path" not in public[-2]["metadata"]
    assert _allowed(detail) == {"start"}


@pytest.mark.anyio
async def test_reset_refused_while_an_execution_owns_the_task(harness: Harness) -> None:
    harness.executor.gated = True
    async with _client(harness.app) as client:
        await _act(client, "start", client_action_id=KEY)
        harness.executor.started.get(timeout=30)
        response = await _act(client, "reset", confirm=True)
        harness.executor.permits.release()
    assert response.status_code == 409
    assert "pause it first" in response.json()["detail"]
    assert harness.context.workspaces.exists("DEMO-1")


# ---------------------------------------------------------------- availability / identity


@pytest.mark.anyio
async def test_backend_reports_action_availability_per_state(harness: Harness) -> None:
    async with _client(harness.app) as client:
        ready = (await client.get("/api/tasks/DEMO-1")).json()
        _waiting_for_human(harness.context)
        waiting = (await client.get("/api/tasks/DEMO-1")).json()
        harness.context.sessions.pause("DEMO-1", _cli_human())
        paused = (await client.get("/api/tasks/DEMO-1")).json()

    assert _allowed(ready) == {"start"}
    assert ready["actions"]["approve"]["reason"] == (
        "Task cannot be approved until it is waiting for human review."
    )
    assert _allowed(waiting) == {"pause", "approve", "reject", "reset"}
    assert _allowed(paused) == {"resume", "reset"}
    assert paused["actions"]["pause"]["reason"] == "Task is already paused."


@pytest.mark.anyio
async def test_viewer_is_read_only_and_runner_is_required(tmp_path: Path) -> None:
    viewer = Harness(tmp_path / "a")
    _waiting_for_human(viewer.context)
    async with _client(viewer.app, "watcher", Role.VIEWER) as client:
        detail = (await client.get("/api/tasks/DEMO-1")).json()
        responses = [
            await _act(client, "start", client_action_id=KEY),
            await _act(client, "pause"),
            await _act(client, "approve"),
            await _act(client, "reject", client_action_id=KEY, message="no"),
            await _act(client, "resume", client_action_id=KEY),
            await _act(client, "reset", confirm=True),
        ]
    assert _allowed(detail) == set()
    assert detail["actions"]["approve"]["reason"].startswith("Read-only access")
    assert [response.status_code for response in responses] == [403] * 6
    assert viewer.status() is TaskStatus.WAITING_FOR_HUMAN

    context = _context(tmp_path / "b", FakeAgentExecutor())
    async with _client(create_app(context)) as client:  # runner disabled
        detail = (await client.get("/api/tasks/DEMO-1")).json()
        start = await _act(client, "start", client_action_id=KEY)
        spoof = await _act(client, "start", client_action_id=KEY, actor_id="admin")
        command = await _act(client, "pause", command="rm -rf /")
    assert start.status_code == 503 and "AI_PLATFORM_ENABLE_RUNNER" in start.json()["detail"]
    assert "AI_PLATFORM_ENABLE_RUNNER" in detail["actions"]["start"]["reason"]
    assert context.storage.get_task("DEMO-1").active_execution is None  # nothing taken
    assert spoof.status_code == 422
    assert command.status_code == 409  # body ignored; pause itself is invalid on READY
    assert _events(context, "DEMO-1", EventType.TASK_STARTED) == []


@pytest.mark.anyio
async def test_foreign_lock_errors_do_not_leak_internals(harness: Harness) -> None:
    _waiting_for_human(harness.context)
    assert harness.context.storage.try_acquire_execution(
        "DEMO-1",
        ExecutionKind.HUMAN_SHELL,
        "human_shell:secret-host:4242:x",
        allowed_statuses={TaskStatus.WAITING_FOR_HUMAN},
        actor_id="alex",
        process_id=4242,
        hostname="secret-host",
    )
    async with _client(harness.app) as client:
        reject = await _act(client, "reject", client_action_id=KEY, message="redo")
        detail = (await client.get("/api/tasks/DEMO-1")).text

    assert reject.status_code == 409
    assert reject.json()["detail"] == "Another execution currently owns this task."
    for secret in ("secret-host", "4242"):
        assert secret not in reject.text and secret not in detail
    harness.context.storage.release_execution("DEMO-1", "human_shell:secret-host:4242:x")


@pytest.mark.anyio
async def test_full_browser_lifecycle_start_chat_pause_resume_approve(
    harness: Harness,
) -> None:
    async with _client(harness.app) as client:
        await _act(client, "start", client_action_id="life-start-01")
        _wait_for(lambda: harness.status() is TaskStatus.WAITING_FOR_HUMAN and harness.idle())
        await client.post(
            "/api/tasks/DEMO-1/messages",
            json={"message": "Double-check the tests.", "client_message_id": "life-chat-01"},
        )
        _wait_for(lambda: _statuses(harness.context) == [MessageStatus.COMPLETED])
        _wait_for(lambda: harness.status() is TaskStatus.WAITING_FOR_HUMAN and harness.idle())
        assert (await _act(client, "pause")).status_code == 200
        await _act(client, "resume", client_action_id="life-resume-1")
        _wait_for(lambda: harness.status() is TaskStatus.WAITING_FOR_HUMAN and harness.idle())
        approve = await _act(client, "approve")
        messages = (await client.get("/api/tasks/DEMO-1/messages")).json()

    assert approve.status_code == 200 and harness.status() is TaskStatus.COMPLETED
    assert [r.continuation for r in harness.executor.requests] == [False, True, True]
    assert any(m["content"] == "Double-check the tests." for m in messages)
    assert harness.executor.max_active == 1


def _cli_human() -> HumanIdentity:
    return HumanIdentity(actor_id="cli-user", display_name="cli-user")
