"""Demo 2.5 Phase 6: Chat is a projection over durable workflow data.

Covers the core Phase 6 invariant -- phase output and waiting-for-human
questions must appear directly in Chat, never only in Activity/Trace -- plus
Activity's newest-first presentation and Chat surviving a process restart.
Every test uses isolated storage/workspaces and a fake executor; none calls
Claude.
"""

import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from ai_platform.api.app import create_app
from ai_platform.application import ApplicationContext, create_application_context
from ai_platform.auth import Role
from ai_platform.config import Settings
from ai_platform.events import ActorType, Event, EventType
from ai_platform.identity import HumanIdentity
from ai_platform.runner import TaskTurnRunner
from ai_platform.workflow import WorkflowPhase
from tests.fakes import CLEAN_PLAN_PAYLOAD, FakeAgentExecutor

WEB_ACTOR = "web-tester"


def _reset_to_brainstorm(context: ApplicationContext, task_id: str = "DEMO-1") -> None:
    """Tasks default to IMPLEMENTATION (the legacy/manual-task phase); a
    browser-created task starts at BRAINSTORM (task_creation.py). Match that
    for tests exercising the full read-only pipeline, the same way
    test_phase3_pipeline.py's ``_managed_task`` does."""

    context.storage.transition_workflow_phase(
        task_id,
        WorkflowPhase.IMPLEMENTATION,
        WorkflowPhase.BRAINSTORM,
        Event(
            task_id=task_id,
            event_type=EventType.WORKFLOW_PHASE_CHANGED,
            actor_type=ActorType.SYSTEM,
            actor_id="test-setup",
            metadata={
                "from_phase": "IMPLEMENTATION",
                "to_phase": "BRAINSTORM",
                "transition_mode": "MANUAL",
            },
        ),
    )


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    root = Path(__file__).parents[1]
    settings = Settings(
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
        verification_timeout_seconds=30,
        max_attempts_per_tier=1,
        lock_heartbeat_seconds=1,
        lock_stale_seconds=10,
    )
    return dataclasses.replace(settings, **overrides)


def _context(
    tmp_path: Path, executor: FakeAgentExecutor | None = None, **overrides: object
) -> ApplicationContext:
    chosen = executor or FakeAgentExecutor()
    return create_application_context(
        _settings(tmp_path, **overrides), executor_factory=lambda: chosen
    )


def provision(app, username: str = WEB_ACTOR, role: Role = Role.DEVELOPER) -> str:
    auth = app.state.auth
    if username not in {user.username for user in auth.list_users()}:
        auth.add_user(username, username.title(), role)
    return auth.create_token(username).secret


@asynccontextmanager
async def _client(
    app, username: str = WEB_ACTOR, role: Role = Role.DEVELOPER
) -> AsyncIterator[httpx.AsyncClient]:
    token = provision(app, username, role)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/api/auth/login", json={"username": username, "token": token})
        assert login.status_code == 200, login.text
        yield client


def _web_human() -> HumanIdentity:
    return HumanIdentity(actor_id=WEB_ACTOR, display_name=WEB_ACTOR.title())


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _by_type(messages: list[dict], type_: str) -> list[dict]:
    return [m for m in messages if m["type"] == type_]


# ---------------------------------------------------------------- A: Plan success


@pytest.mark.anyio
async def test_brainstorm_and_plan_output_appear_directly_in_chat(tmp_path: Path) -> None:
    """A. Each read-only phase's output is a Chat item, not just a Trace event."""

    context = _context(tmp_path)
    _reset_to_brainstorm(context)
    context.sessions.start("DEMO-1", HumanIdentity(actor_id="cli-user", display_name="cli-user"))

    async with _client(create_app(context)) as client:
        messages = (await client.get("/api/tasks/DEMO-1/messages")).json()

    brainstorm = _by_type(messages, "phase_result")
    kinds = {m["artifact_kind"] for m in brainstorm}
    assert "BRAINSTORM_SUMMARY" in kinds
    assert "PLAN" in kinds
    plan_item = next(m for m in brainstorm if m["artifact_kind"] == "PLAN")
    assert plan_item["title"] == "Claude · Plan"
    assert plan_item["role"] == "agent"
    assert "Implement the described change." in plan_item["content"]
    assert "Steps" in plan_item["content"]
    assert plan_item["artifact_version"] == 1
    assert plan_item["logical_model"] is not None
    # Chronological: Brainstorm's result comes before Plan's.
    by_kind = {m["artifact_kind"]: m["sequence_id"] for m in brainstorm}
    assert by_kind["BRAINSTORM_SUMMARY"] < by_kind["PLAN"]


# ---------------------------------------------------------------- B: Plan needs human


@pytest.mark.anyio
async def test_plan_blocker_shows_as_human_input_required_and_rerun_produces_v2(
    tmp_path: Path,
) -> None:
    """B. A blocking Plan gate shows an explicit question in Chat; answering it
    reruns the same phase and a new artifact version appears in Chat."""

    blocked_plan = {**CLEAN_PLAN_PAYLOAD, "open_questions": ["Should X support Y?"]}
    executor = FakeAgentExecutor(plan_payload=blocked_plan)
    context = _context(tmp_path, executor)
    _reset_to_brainstorm(context)
    context.sessions.start("DEMO-1", HumanIdentity(actor_id="cli-user", display_name="cli-user"))
    assert context.storage.get_task("DEMO-1").workflow_phase.value == "PLAN"

    async with _client(create_app(context)) as client:
        messages = (await client.get("/api/tasks/DEMO-1/messages")).json()
        needs_input = _by_type(messages, "human_input_required")
        assert len(needs_input) == 1
        item = needs_input[0]
        assert item["title"] == "Needs your input · Plan"
        assert item["requires_human_input"] is True
        assert item["workflow_phase"] == "PLAN"
        assert any(check["key"] == "open_questions_resolved" for check in item["blocking_checks"])
        assert "Questions:" in item["content"]
        assert "Readiness:" in item["content"]

        # The human answers directly in Chat -- a normal durable message, not a
        # separate "answer question" mechanism -- and the same phase reruns.
        executor.plan_payload = CLEAN_PLAN_PAYLOAD  # the next attempt resolves cleanly
        response = await client.post(
            "/api/tasks/DEMO-1/messages",
            json={"message": "X should support Y.", "client_message_id": "answer-plan-001"},
        )
        assert response.status_code == 202
        TaskTurnRunner(context.sessions, context.storage).run_pending()

        messages_after = (await client.get("/api/tasks/DEMO-1/messages")).json()

    plan_results = [
        m for m in messages_after if m["type"] == "phase_result" and m["artifact_kind"] == "PLAN"
    ]
    assert [m["artifact_version"] for m in plan_results] == [1, 2]
    activity = _by_type(messages_after, "platform_activity")
    assert any("Plan passed readiness gate" in a["content"] for a in activity)
    # The original question is still there -- Chat is an append-only timeline.
    assert len(_by_type(messages_after, "human_input_required")) == 1
    assert context.storage.get_task("DEMO-1").workflow_phase.value != "PLAN"


# ---------------------------------------------------------------- C: full pipeline


def _clean_pipeline_context(tmp_path: Path, executor: FakeAgentExecutor) -> ApplicationContext:
    """A task whose Implementation attempt is fixed on the first try, so a single
    ``start()`` flows Brainstorm -> Plan -> Implementation -> Review -> Human
    Review in one turn, matching the documented happy path."""

    from ai_platform.models import LogicalModel
    from ai_platform.workflow import WorkflowPhase

    context = _context(tmp_path, executor)
    context.storage.set_phase_model_preference(
        "DEMO-1",
        WorkflowPhase.IMPLEMENTATION,
        LogicalModel.CLAUDE_SONNET,
        "test-setup",
        "test-setup",
    )
    return context


@pytest.mark.anyio
async def test_review_output_appears_in_chat(tmp_path: Path) -> None:
    """C. Review's output is a Chat item too, once the pipeline reaches it."""

    context = _clean_pipeline_context(tmp_path, FakeAgentExecutor(fix_on_attempt=1))
    context.sessions.start("DEMO-1", HumanIdentity(actor_id="cli-user", display_name="cli-user"))
    assert context.storage.get_task("DEMO-1").workflow_phase.value == "HUMAN_REVIEW"

    async with _client(create_app(context)) as client:
        messages = (await client.get("/api/tasks/DEMO-1/messages")).json()

    review_items = [
        m for m in messages if m["type"] == "phase_result" and m["artifact_kind"] == "REVIEW_REPORT"
    ]
    assert len(review_items) == 1
    assert review_items[0]["title"] == "Claude · Review"
    assert "No issues found." in review_items[0]["content"]


# ---------------------------------------------------------------- D: Human Review


@pytest.mark.anyio
async def test_human_review_message_does_not_start_a_turn(tmp_path: Path) -> None:
    """D. A normal Chat message during Human Review stays durable but never runs AI."""

    executor = FakeAgentExecutor(fix_on_attempt=1)
    context = _clean_pipeline_context(tmp_path, executor)
    context.sessions.start("DEMO-1", HumanIdentity(actor_id="cli-user", display_name="cli-user"))
    assert context.storage.get_task("DEMO-1").workflow_phase.value == "HUMAN_REVIEW"
    turns_before = len(executor.requests)

    async with _client(create_app(context)) as client:
        response = await client.post(
            "/api/tasks/DEMO-1/messages",
            json={"message": "Looks fine to me.", "client_message_id": "hr-msg-001"},
        )
        assert response.status_code == 202
        TaskTurnRunner(context.sessions, context.storage).run_pending()
        messages = (await client.get("/api/tasks/DEMO-1/messages")).json()

    assert len(executor.requests) == turns_before  # no agent turn ran
    assert any(m["content"] == "Looks fine to me." for m in messages)
    review_items = [
        m for m in messages if m["type"] == "phase_result" and m["artifact_kind"] == "REVIEW_REPORT"
    ]
    assert len(review_items) == 1  # Review's output is visible in the same Chat


# ---------------------------------------------------------------- E: restart


@pytest.mark.anyio
async def test_chat_reconstructs_from_durable_state_after_restart(tmp_path: Path) -> None:
    """E. Reopening a task after a process restart shows the same Chat, unabridged."""

    blocked_plan = {**CLEAN_PLAN_PAYLOAD, "open_questions": ["Should X support Y?"]}
    first = _context(tmp_path, FakeAgentExecutor(plan_payload=blocked_plan))
    _reset_to_brainstorm(first)
    first.sessions.start("DEMO-1", HumanIdentity(actor_id="cli-user", display_name="cli-user"))
    async with _client(create_app(first)) as client:
        before = (await client.get("/api/tasks/DEMO-1/messages")).json()

    restarted = _context(tmp_path, FakeAgentExecutor(plan_payload=blocked_plan))
    async with _client(create_app(restarted)) as client:
        after = (await client.get("/api/tasks/DEMO-1/messages")).json()

    assert [(m["type"], m["id"]) for m in before] == [(m["type"], m["id"]) for m in after]
    assert any(m["type"] == "human_input_required" for m in after)
    assert any(m["type"] == "phase_result" for m in after)


# ---------------------------------------------------------------- Activity ordering


@pytest.mark.anyio
async def test_activity_order_param_reverses_presentation_not_storage(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context.sessions.start("DEMO-1", HumanIdentity(actor_id="cli-user", display_name="cli-user"))

    async with _client(create_app(context)) as client:
        ascending = (await client.get("/api/tasks/DEMO-1/events")).json()
        explicit_asc = (await client.get("/api/tasks/DEMO-1/events?order=asc")).json()
        descending = (await client.get("/api/tasks/DEMO-1/events?order=desc")).json()

    assert len(ascending) > 1
    assert ascending == explicit_asc
    assert [e["sequence_id"] for e in ascending] == sorted(e["sequence_id"] for e in ascending)
    assert descending == list(reversed(ascending))
    # The canonical event log itself is untouched: still strictly increasing.
    sequence_ids = [e.sequence_id for e in context.storage.get_events("DEMO-1")]
    assert sequence_ids == sorted(sequence_ids)
