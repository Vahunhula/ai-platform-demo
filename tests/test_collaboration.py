"""Phase 3 shared TaskSession collaboration tests; no real Claude calls."""

import getpass
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event as ThreadEvent

import pytest
from typer.testing import CliRunner

from ai_platform.cli import app
from ai_platform.config import Settings
from ai_platform.events import ActorType, Event, EventType
from ai_platform.executors.base import ExecutionRequest, ExecutionResult
from ai_platform.identity import HumanIdentity, LocalIdentityProvider
from ai_platform.models import ModelTier, TaskStatus
from ai_platform.router import ModelRouter
from ai_platform.sessions import TaskSessionError, TaskSessionService
from ai_platform.storage import SQLiteStorage
from ai_platform.task_loader import get_task, load_tasks
from ai_platform.workspace import LocalWorkspaceProvider
from tests.fakes import FakeAgentExecutor


class BlockingFakeAgentExecutor(FakeAgentExecutor):
    """Hold one continuation turn open so another process can interact with it."""

    def __init__(self) -> None:
        super().__init__()
        self.started = ThreadEvent()
        self.release = ThreadEvent()

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.requests.append(request)
        self.started.set()
        if not self.release.wait(timeout=10):
            raise RuntimeError("test did not release blocking executor")
        return ExecutionResult(succeeded=True, summary="Blocking fake completed")


def _human(actor_id: str) -> HumanIdentity:
    return HumanIdentity(actor_id=actor_id, display_name=actor_id.title())


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


def _service(
    tmp_path: Path,
    executor: FakeAgentExecutor,
) -> tuple[TaskSessionService, SQLiteStorage, LocalWorkspaceProvider]:
    settings = _settings(tmp_path)
    definitions = load_tasks(settings.tasks_path)
    storage = SQLiteStorage(settings.db_path)
    storage.initialize()
    for task in definitions:
        storage.create_task(task)
    workspaces = LocalWorkspaceProvider(settings.workspace_root, settings.demo_repository)
    router = ModelRouter(
        {
            ModelTier.CHEAP: "haiku-test",
            ModelTier.DEFAULT: "sonnet-test",
            ModelTier.STRONG: "opus-test",
        }
    )
    service = TaskSessionService(
        settings,
        definitions,
        storage,
        workspaces,
        router,
        lambda: executor,
    )
    return service, storage, workspaces


def test_local_identity_prefers_development_override_and_falls_back_to_os(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LocalIdentityProvider()
    monkeypatch.setenv("AI_PLATFORM_USER", "alex")

    assert provider.get_current_user() == HumanIdentity(actor_id="alex", display_name="alex")

    monkeypatch.delenv("AI_PLATFORM_USER")
    monkeypatch.setattr(getpass, "getuser", lambda: "os-user")
    assert provider.get_current_user().actor_id == "os-user"


def test_events_are_ordered_append_only_and_safe_under_concurrent_writes(
    tmp_path: Path,
) -> None:
    task = get_task(load_tasks(Path(__file__).parents[1] / "tasks.json"), "DEMO-1")
    storage = SQLiteStorage(tmp_path / "platform.db")
    storage.initialize()
    storage.create_task(task)

    def append(index: int) -> None:
        storage.append_event(
            Event(
                task_id=task.id,
                event_type=EventType.HUMAN_MESSAGE,
                actor_type=ActorType.HUMAN,
                actor_id=f"user-{index % 4}",
                metadata={"message": str(index)},
            )
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(40)))

    events = storage.get_events(task.id)
    sequences = [event.sequence_id for event in events]
    assert len(events) == 40
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == 40
    with sqlite3.connect(storage.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE events SET actor_id = 'changed'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM events")


def test_follow_cursor_yields_each_new_event_once(tmp_path: Path) -> None:
    service, storage, _workspaces = _service(tmp_path, FakeAgentExecutor())
    cursor = max(
        (event.sequence_id or 0 for event in storage.get_events("DEMO-1")),
        default=0,
    )
    first = service.connect("DEMO-1", _human("alex"))
    stream = service.follow_events("DEMO-1", cursor, poll_interval_seconds=0.01)

    assert next(stream).sequence_id == first.sequence_id
    second = service.connect("DEMO-1", _human("david"))
    assert next(stream).sequence_id == second.sequence_id
    assert storage.get_events_after("DEMO-1", second.sequence_id or 0) == []
    stream.close()


def test_attach_renders_shared_participants_and_conversation(tmp_path: Path) -> None:
    service, _storage, _workspaces = _service(tmp_path, FakeAgentExecutor())
    service.message("DEMO-1", "Check the typo.", _human("alex"))
    service.message("DEMO-1", "Keep the fix small.", _human("david"))
    settings = service.settings
    env = {
        "AI_PLATFORM_USER": "vakho",
        "AI_PLATFORM_DATA_DIR": str(settings.data_dir),
        "AI_PLATFORM_WORKSPACE_ROOT": str(settings.workspace_root),
        "AI_PLATFORM_DB_PATH": str(settings.db_path),
        "AI_PLATFORM_CHECKPOINT_DB_PATH": str(settings.checkpoint_db_path),
    }

    result = CliRunner().invoke(app, ["attach", "DEMO-1"], env=env)

    assert result.exit_code == 0
    assert "Participants seen in durable history" in result.stdout
    assert "alex" in result.stdout
    assert "david" in result.stdout
    assert "vakho" in result.stdout
    assert "Check the typo." in result.stdout
    assert "Keep the fix small." in result.stdout


def test_shared_takeover_resume_reject_and_human_approval(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "demo_repo" / "app" / "messages.py"
    source_before = source.read_bytes()
    executor = FakeAgentExecutor()
    service, storage, workspaces = _service(tmp_path, executor)
    vakho = _human("vakho")
    alex = _human("alex")
    david = _human("david")

    service.start("DEMO-1", vakho)
    workspace = workspaces.get_path("DEMO-1")
    service.message("DEMO-1", "Check for the typo elsewhere.", alex)
    assert len(executor.requests) == 2
    assert executor.requests[-1].continuation is True
    assert any("alex:" in message for message in executor.requests[-1].human_messages)

    assert service.pause("DEMO-1", david) is False
    assert storage.get_task("DEMO-1").status is TaskStatus.PAUSED_BY_HUMAN
    request_count = len(executor.requests)

    def manual_edit(path: Path) -> int:
        target = path / "app" / "messages.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# reviewed manually\n")
        return 0

    changes = service.shell("DEMO-1", david, runner=manual_edit)
    assert [(change.path, change.change_type) for change in changes] == [
        ("app/messages.py", "modified")
    ]
    assert len(executor.requests) == request_count

    service.resume("DEMO-1", david, "Review my manual change and continue.")
    assert executor.requests[-1].workspace_path == workspace
    assert executor.requests[-1].human_workspace_changed is True
    assert executor.requests[-1].continuation is True
    assert (workspace / "app" / "messages.py").read_text(encoding="utf-8").endswith(
        "# reviewed manually\n"
    )

    service.reject("DEMO-1", "Please keep the implementation simple.", alex)
    assert executor.requests[-1].workspace_path == workspace
    service.approve("DEMO-1", vakho)

    record = storage.get_task("DEMO-1")
    assert record is not None
    assert record.status is TaskStatus.COMPLETED
    events = storage.get_events("DEMO-1")
    assert [event.sequence_id for event in events] == sorted(
        event.sequence_id for event in events
    )
    assert any(
        event.event_type is EventType.HUMAN_WORKSPACE_CHANGED
        and event.actor_id == "david"
        and event.metadata["files"] == [
            {"path": "app/messages.py", "change_type": "modified"}
        ]
        for event in events
    )
    assert any(
        event.event_type is EventType.HUMAN_MESSAGE
        and event.actor_id == "david"
        and event.metadata["message"] == "Review my manual change and continue."
        for event in events
    )
    assert any(
        event.event_type is EventType.HUMAN_REJECTED and event.actor_id == "alex"
        for event in events
    )
    assert any(
        event.event_type is EventType.HUMAN_APPROVED and event.actor_id == "vakho"
        for event in events
    )
    assert source.read_bytes() == source_before


def test_second_message_cannot_race_agent_and_pause_stops_next_turn(tmp_path: Path) -> None:
    initial = FakeAgentExecutor()
    service, storage, workspaces = _service(tmp_path, initial)
    service.start("DEMO-1", _human("vakho"))
    blocking = BlockingFakeAgentExecutor()
    service.executor_factory = lambda: blocking

    with ThreadPoolExecutor(max_workers=1) as pool:
        first_turn = pool.submit(
            service.message,
            "DEMO-1",
            "Check X.",
            _human("alex"),
        )
        assert blocking.started.wait(timeout=10)
        second = service.message("DEMO-1", "Also check Y.", _human("david"))
        assert second.agent_started is False
        assert second.queued is True
        assert len(blocking.requests) == 1
        with pytest.raises(TaskSessionError, match="modified by Claude"):
            service.shell("DEMO-1", _human("david"), runner=lambda _path: 0)

        assert service.pause("DEMO-1", _human("david")) is True
        paused_record = storage.get_task("DEMO-1")
        assert paused_record is not None
        assert paused_record.pause_requested is True
        assert paused_record.agent_running is True
        blocking.release.set()
        outcome = first_turn.result(timeout=15)

    assert outcome.state["status"] == TaskStatus.PAUSED_BY_HUMAN.value
    final = storage.get_task("DEMO-1")
    assert final is not None
    assert final.status is TaskStatus.PAUSED_BY_HUMAN
    assert final.active_execution is None
    assert len(blocking.requests) == 1
    assert workspaces.exists("DEMO-1")


def test_parallel_cli_processes_write_same_sqlite_without_corruption(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    settings = _settings(tmp_path)
    base_env = {
        **os.environ,
        "AI_PLATFORM_DATA_DIR": str(settings.data_dir),
        "AI_PLATFORM_WORKSPACE_ROOT": str(settings.workspace_root),
        "AI_PLATFORM_DB_PATH": str(settings.db_path),
        "AI_PLATFORM_CHECKPOINT_DB_PATH": str(settings.checkpoint_db_path),
        "PYTHONIOENCODING": "utf-8",
    }

    def write_from_process(index: int) -> subprocess.CompletedProcess[str]:
        environment = {**base_env, "AI_PLATFORM_USER": f"process-{index}"}
        return subprocess.run(
            [sys.executable, "-m", "ai_platform.cli", "message", "DEMO-1", f"note-{index}"],
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            shell=False,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write_from_process, range(4)))

    assert [result.returncode for result in results] == [0, 0, 0, 0], [
        (result.returncode, result.stdout, result.stderr) for result in results
    ]
    storage = SQLiteStorage(settings.db_path)
    storage.initialize()
    messages = [
        event
        for event in storage.get_events("DEMO-1")
        if event.event_type is EventType.HUMAN_MESSAGE
    ]
    assert {event.metadata["message"] for event in messages} == {
        "note-0",
        "note-1",
        "note-2",
        "note-3",
    }
