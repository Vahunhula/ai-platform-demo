"""Bounded isolated Phase 7 soak; never uses a real model or production paths."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from time import monotonic

import httpx

from ai_platform.api.app import create_app
from ai_platform.auth import Role
from ai_platform.events import ActorType, Event, EventType
from ai_platform.workflow import WorkflowPhase
from tests.fakes import FakeAgentExecutor
from tests.test_messaging import _context


async def soak(seconds: float) -> None:
    root = Path(__file__).parents[1]
    with tempfile.TemporaryDirectory(prefix="demo25-soak-") as temporary:
        runtime = Path(temporary)
        context = _context(runtime, FakeAgentExecutor())
        app = create_app(context)
        auth = app.state.auth
        user = auth.add_user("soak", "Soak User", Role.DEVELOPER)
        token = auth.create_token("soak").secret
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        repository = context.repositories.register_local(
            "soak", "Soak Repository", root / "demo_repo", branch
        )
        transport = httpx.ASGITransport(app=app)
        operations = 0
        removals = 0
        cursor = 0
        deadline = monotonic() + seconds
        async with httpx.AsyncClient(transport=transport, base_url="http://soak") as client:
            login = await client.post(
                "/api/auth/login", json={"username": "soak", "token": token}
            )
            assert login.status_code == 200
            while monotonic() < deadline:
                suffix = f"{operations:012d}"
                responses = await asyncio.gather(
                    client.get("/api/tasks"),
                    client.get("/api/tasks/DEMO-1"),
                    client.get("/api/tasks/DEMO-1/events/page?limit=50"),
                    client.get("/api/tasks/DEMO-1/messages"),
                    client.post(
                        "/api/tasks/DEMO-1/commands",
                        json={
                            "command_text": "/status",
                            "client_command_id": f"soak-status-{suffix}",
                        },
                    ),
                    client.post(
                        "/api/tasks/DEMO-1/claude-commands",
                        json={
                            "command_text": "claude/status",
                            "client_command_id": f"soak-claude-{suffix}",
                        },
                    ),
                )
                assert all(response.status_code == 200 for response in responses)

                # Exercise the same durable cursor used by SSE reconnect.
                streamed = context.storage.get_events_after("DEMO-1", cursor)
                if streamed:
                    cursor = streamed[-1].sequence_id or cursor

                if operations % 100 == 0:
                    created = await client.post(
                        "/api/tasks",
                        json={
                            "title": f"Disposable soak {operations}",
                            "description": "Isolated removal cycle.",
                            "repository_id": repository.id,
                            "base_branch": repository.default_branch,
                            "assignee_user_id": user.user_id,
                            "jira_key": None,
                        },
                    )
                    assert created.status_code == 201
                    task_id = created.json()["id"]
                    with context.storage.transaction(immediate=True) as connection:
                        connection.execute(
                            "UPDATE tasks SET status = ?, workflow_phase = ? WHERE task_id = ?",
                            ("waiting_for_human", WorkflowPhase.HUMAN_REVIEW.value, task_id),
                        )
                    assert (await client.post(f"/api/tasks/{task_id}/defer")).status_code == 200
                    assert (await client.delete(f"/api/tasks/{task_id}")).status_code == 204
                    removals += 1

                context.storage.append_event(
                    Event(
                        task_id="DEMO-2",
                        event_type=EventType.STATUS_CHANGED,
                        actor_type=ActorType.SYSTEM,
                        actor_id="soak",
                        metadata={"iteration": operations},
                    )
                )
                operations += 1

        records = context.storage.list_tasks()
        assert all(record.active_execution is None for record in records)
        assert sum(context.storage.pending_message_count(record.task_id) for record in records) == 0
        connection = sqlite3.connect(context.settings.db_path)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        connection.close()
        assert integrity == "ok"
        print(
            f"soak ok: duration={seconds:.1f}s operations={operations} "
            f"removals={removals} writers=0 queued=0 integrity={integrity}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=600.0)
    arguments = parser.parse_args()
    if arguments.seconds <= 0:
        parser.error("--seconds must be positive")
    asyncio.run(soak(arguments.seconds))


if __name__ == "__main__":
    main()
