"""One disposable, deterministic Demo 2.5 lifecycle including restart and removal."""

import subprocess
from pathlib import Path

import pytest

from ai_platform.api.app import create_app
from ai_platform.events import EventType
from ai_platform.executors.base import ExecutionRequest
from ai_platform.identity import HumanIdentity
from ai_platform.runner import TaskTurnRunner
from ai_platform.workflow import WorkflowPhase
from tests.fakes import CLEAN_PLAN_PAYLOAD, FakeAgentExecutor
from tests.test_messaging import WEB_ACTOR, _client, _context


class FinalDemoExecutor(FakeAgentExecutor):
    """Add one task-specific passing feature without repairing baseline failures."""

    def execute(self, request: ExecutionRequest):  # noqa: ANN201
        if request.phase is WorkflowPhase.IMPLEMENTATION:
            (request.workspace_path / "app" / "final_demo.py").write_text(
                "def ready(): return True\n", encoding="utf-8"
            )
            (request.workspace_path / "tests" / "test_final_demo.py").write_text(
                "from app.final_demo import ready\n\ndef test_ready(): assert ready()\n",
                encoding="utf-8",
            )
        return super().execute(request)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _branch(root: Path) -> str:
    return subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.anyio
async def test_final_demo_waiting_restart_confirm_and_remove(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    blocked = {**CLEAN_PLAN_PAYLOAD, "open_questions": ["Use a boolean readiness API?"]}
    first_executor = FinalDemoExecutor(plan_payload=blocked)
    first = _context(tmp_path, first_executor)
    app = create_app(first)
    repository = first.repositories.register_local(
        "final-demo", "Final Demo", root / "tests" / "fixtures" / "demo_repo", _branch(root)
    )
    source_before = (root / "tests" / "fixtures" / "demo_repo" / "app" / "messages.py").read_bytes()
    task_b_events = list(first.storage.get_events("DEMO-2"))

    async with _client(app) as client:
        assignee = next(user for user in app.state.auth.list_users() if user.username == WEB_ACTOR)
        created_response = await client.post(
            "/api/tasks",
            json={
                "title": "Final freeze scenario",
                "description": "Prove the complete Demo 2.5 lifecycle.",
                "repository_id": repository.id,
                "base_branch": repository.default_branch,
                "assignee_user_id": assignee.user_id,
                "jira_key": None,
            },
        )
        assert created_response.status_code == 201
        task_id = created_response.json()["id"]

    first.sessions.start(task_id, HumanIdentity(actor_id=WEB_ACTOR, display_name="Web Tester"))
    waiting = first.storage.get_task(task_id)
    assert waiting is not None
    assert waiting.workflow_phase is WorkflowPhase.PLAN
    assert any(
        event.event_type is EventType.WORKFLOW_PHASE_GATE_EVALUATED
        for event in first.storage.get_events(task_id)
    )

    # A new application/context sees the durable question and continues from it.
    resumed_executor = FinalDemoExecutor()
    restarted = _context(tmp_path, resumed_executor)
    restarted_app = create_app(restarted)
    async with _client(restarted_app) as client:
        before = (await client.get(f"/api/tasks/{task_id}/messages")).json()
        assert any(item["type"] == "human_input_required" for item in before)
        answer = await client.post(
            f"/api/tasks/{task_id}/messages",
            json={
                "message": "Yes, use a boolean readiness API.",
                "client_message_id": "final-demo-answer-0001",
            },
        )
        assert answer.status_code == 202
        assert TaskTurnRunner(restarted.sessions, restarted.storage).run_pending() == 1

        detail = (await client.get(f"/api/tasks/{task_id}")).json()
        assert detail["workflow_phase"] == "HUMAN_REVIEW"
        assert detail["can_remove"] is False
        assert "Confirm or Defer" in detail["remove_disabled_reason"]
        chat = (await client.get(f"/api/tasks/{task_id}/messages")).json()
        plans = [
            item
            for item in chat
            if item["type"] == "phase_result" and item.get("artifact_kind") == "PLAN"
        ]
        assert [item["artifact_version"] for item in plans] == [1, 2]

        for path, body in (
            (
                "commands",
                {"command_text": "/status", "client_command_id": "final-status-0001"},
            ),
            (
                "claude-commands",
                {"command_text": "claude/help", "client_command_id": "final-help-0001"},
            ),
            (
                "claude-commands",
                {"command_text": "claude/status", "client_command_id": "final-claude-0001"},
            ),
        ):
            assert (await client.post(f"/api/tasks/{task_id}/{path}", json=body)).status_code == 200

        activity = (await client.get(f"/api/tasks/{task_id}/events/page")).json()
        assert len(activity["items"]) <= 50
        assert activity["order"] == "desc"

        confirmed = await client.post(f"/api/tasks/{task_id}/approve")
        assert confirmed.status_code == 200
        removable = (await client.get(f"/api/tasks/{task_id}")).json()
        assert removable["disposition"] == "CONFIRMED"
        assert removable["can_remove"] is True
        assert (await client.delete(f"/api/tasks/{task_id}")).status_code == 204
        assert (await client.get(f"/api/tasks/{task_id}")).status_code == 404

    assert restarted.storage.get_task(task_id, include_removing=True) is None
    assert not restarted.workspaces.exists(task_id)
    assert restarted.storage.get_events("DEMO-2") == task_b_events
    assert (
        root / "tests" / "fixtures" / "demo_repo" / "app" / "messages.py"
    ).read_bytes() == source_before
    with restarted.storage.transaction() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
