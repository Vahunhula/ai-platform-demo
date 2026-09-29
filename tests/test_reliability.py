"""Phase 4 lock, configuration, preflight, and portability tests."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_platform.claude_auth import ClaudeAuthState, ClaudeAuthStatus
from ai_platform.config import Settings
from ai_platform.doctor import CheckStatus, inspect_environment
from ai_platform.events import EventType
from ai_platform.executors.base import AgentAuthenticationError
from ai_platform.executors.claude import ClaudeAgentExecutor
from ai_platform.identity import HumanIdentity
from ai_platform.locks import ExecutionLockManager, LockHealth
from ai_platform.models import ExecutionKind, TaskStatus
from ai_platform.router import ModelRouter
from ai_platform.sessions import TaskSessionError, TaskSessionService
from ai_platform.storage import SQLiteStorage
from ai_platform.task_loader import get_task, load_tasks
from ai_platform.workspace import LocalWorkspaceProvider
from tests.fakes import FakeAgentExecutor


def _settings(tmp_path: Path) -> Settings:
    root = Path(__file__).parents[1]
    return Settings(
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
        verification_timeout_seconds=20,
        max_attempts_per_tier=2,
        lock_heartbeat_seconds=1,
        lock_stale_seconds=10,
    )


def _storage(tmp_path: Path) -> SQLiteStorage:
    root = Path(__file__).parents[1]
    task = get_task(load_tasks(root / "tests" / "fixtures" / "demo_tasks.json"), "DEMO-1")
    storage = SQLiteStorage(tmp_path / "platform.db")
    storage.initialize()
    storage.create_task(task)
    return storage


def _manager(
    storage: SQLiteStorage,
    now: datetime,
    *,
    hostname: str = "demo-host",
    process_id: int = 100,
    process_running: bool | None = True,
) -> ExecutionLockManager:
    return ExecutionLockManager(
        storage,
        heartbeat_seconds=5,
        stale_seconds=60,
        hostname=hostname,
        process_id=process_id,
        clock=lambda: now,
        process_checker=lambda _pid: process_running,
    )


def test_phase3_task_schema_migrates_without_losing_runtime_state(tmp_path: Path) -> None:
    database = tmp_path / "phase3.db"
    now = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, title TEXT NOT NULL, difficulty TEXT NOT NULL,
                status TEXT NOT NULL, selected_tier TEXT, selected_model TEXT,
                attempt INTEGER NOT NULL DEFAULT 0,
                verification_status TEXT NOT NULL DEFAULT 'not_run', workspace_path TEXT,
                active_execution TEXT, execution_owner TEXT, execution_started_at TEXT,
                pause_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO tasks (
                task_id, title, difficulty, status, attempt, verification_status,
                pause_requested, created_at, updated_at
            ) VALUES ('DEMO-1', 'Existing task', 'low', 'ready', 4, 'failed', 0, ?, ?)
            """,
            (now, now),
        )

    storage = SQLiteStorage(database)
    storage.initialize()
    record = storage.get_task("DEMO-1")

    assert record.attempt == 4
    assert record.verification_status.value == "failed"
    assert record.execution_id is None
    assert record.execution_heartbeat_at is None


def test_healthy_lock_cannot_be_stolen(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    started = datetime(2026, 1, 1, tzinfo=UTC)
    owner = _manager(storage, started, process_id=100)
    first = owner.acquire(
        "DEMO-1",
        ExecutionKind.AGENT,
        "claude",
        "execution-one",
        allowed_statuses={TaskStatus.READY},
    )
    contender = _manager(
        storage,
        started + timedelta(seconds=10),
        process_id=200,
        process_running=False,
    )

    second = contender.acquire(
        "DEMO-1",
        ExecutionKind.AGENT,
        "claude",
        "execution-two",
        allowed_statuses={TaskStatus.READY},
    )

    assert first.acquired is True
    assert second.acquired is False
    assert contender.inspect(storage.get_task("DEMO-1")).health is LockHealth.ACTIVE
    assert storage.get_task("DEMO-1").execution_id == "execution-one"


def test_heartbeat_updates_owned_lock(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    manager = ExecutionLockManager(
        storage,
        heartbeat_seconds=5,
        stale_seconds=60,
        hostname="demo-host",
        process_id=100,
        clock=lambda: current[0],
        process_checker=lambda _pid: True,
    )
    lock = manager.acquire(
        "DEMO-1",
        ExecutionKind.AGENT,
        "claude",
        "execution-one",
        allowed_statuses={TaskStatus.READY},
    )
    current[0] += timedelta(seconds=5)

    assert manager.refresh_heartbeat("DEMO-1", lock.owner_token) is True
    assert storage.get_task("DEMO-1").execution_heartbeat_at == current[0]


def test_stale_crashed_lock_recovers_to_paused_with_audit_event(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    started = datetime(2026, 1, 1, tzinfo=UTC)
    owner = _manager(storage, started, process_id=100)
    owner.acquire(
        "DEMO-1",
        ExecutionKind.AGENT,
        "claude",
        "crashed-execution",
        allowed_statuses={TaskStatus.READY},
    )
    storage.update_task_status("DEMO-1", TaskStatus.IMPLEMENTING)
    recovery = _manager(
        storage,
        started + timedelta(seconds=61),
        process_id=200,
        process_running=False,
    )

    assert recovery.recover_all_stale_locks(recovered_by="startup@test") == ["DEMO-1"]
    record = storage.get_task("DEMO-1")
    assert record.status is TaskStatus.PAUSED_BY_HUMAN
    assert record.active_execution is None
    event = next(
        event
        for event in storage.get_events("DEMO-1")
        if event.event_type is EventType.STALE_LOCK_RECOVERED
    )
    assert event.metadata["previous_execution_id"] == "crashed-execution"
    assert event.metadata["previous_pid"] == 100
    assert event.metadata["recovered_by"] == "startup@test"


def test_stale_paused_lock_is_recovered_and_reacquired(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    storage.update_task_status("DEMO-1", TaskStatus.PAUSED_BY_HUMAN)
    started = datetime(2026, 1, 1, tzinfo=UTC)
    owner = _manager(storage, started, process_id=100)
    owner.acquire(
        "DEMO-1",
        ExecutionKind.HUMAN_SHELL,
        "alex",
        "old-shell",
        allowed_statuses={TaskStatus.PAUSED_BY_HUMAN},
    )
    contender = _manager(
        storage,
        started + timedelta(seconds=61),
        process_id=200,
        process_running=False,
    )

    lock = contender.acquire(
        "DEMO-1",
        ExecutionKind.HUMAN_SHELL,
        "david",
        "new-shell",
        allowed_statuses={TaskStatus.PAUSED_BY_HUMAN},
    )

    assert lock.acquired is True
    assert lock.recovered_stale_lock is True
    assert storage.get_task("DEMO-1").execution_id == "new-shell"


def test_expired_remote_host_lock_is_not_stolen(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    started = datetime(2026, 1, 1, tzinfo=UTC)
    owner = _manager(storage, started, hostname="other-host", process_id=100)
    owner.acquire(
        "DEMO-1",
        ExecutionKind.AGENT,
        "claude",
        "remote-execution",
        allowed_statuses={TaskStatus.READY},
    )
    contender = _manager(
        storage,
        started + timedelta(seconds=120),
        hostname="demo-host",
        process_id=200,
        process_running=False,
    )

    lock = contender.acquire(
        "DEMO-1",
        ExecutionKind.AGENT,
        "claude",
        "new-execution",
        allowed_statuses={TaskStatus.READY},
    )

    assert lock.acquired is False
    assert contender.inspect(storage.get_task("DEMO-1")).health is LockHealth.UNVERIFIABLE
    assert EventType.STALE_LOCK_RECOVERED not in {
        event.event_type for event in storage.get_events("DEMO-1")
    }


def test_service_releases_lock_on_success_and_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    definitions = load_tasks(settings.tasks_path)
    storage = SQLiteStorage(settings.db_path)
    storage.initialize()
    for task in definitions:
        storage.create_task(task)
    workspaces = LocalWorkspaceProvider(settings.workspace_root, settings.demo_repository)
    service = TaskSessionService(
        settings,
        definitions,
        storage,
        workspaces,
        ModelRouter.from_settings(settings),
        FakeAgentExecutor,
    )
    human = HumanIdentity(actor_id="alex", display_name="Alex")

    service.start("DEMO-1", human)
    assert storage.get_task("DEMO-1").active_execution is None

    def explode(*_args, **_kwargs):
        raise RuntimeError("simulated graph crash")

    monkeypatch.setattr("ai_platform.sessions.run_task_graph", explode)
    with pytest.raises(RuntimeError, match="simulated graph crash"):
        service.start("DEMO-2", human)
    failed = storage.get_task("DEMO-2")
    assert failed.active_execution is None
    assert failed.status is TaskStatus.FAILED


def test_configured_paths_and_lock_policy_are_portable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_PLATFORM_TASK_FILE", "configuration/tasks.json")
    monkeypatch.setenv("AI_PLATFORM_DEMO_REPO", "sources/demo")
    monkeypatch.setenv("AI_PLATFORM_DATA_DIR", "server/state")
    monkeypatch.setenv("AI_PLATFORM_WORKSPACE_ROOT", "server/workspaces")
    monkeypatch.setenv("AI_PLATFORM_DB_PATH", "server/state/custom.db")
    monkeypatch.setenv("AI_PLATFORM_MAX_ATTEMPTS_PER_TIER", "3")
    monkeypatch.setenv("AI_PLATFORM_LOCK_HEARTBEAT_SECONDS", "7")
    monkeypatch.setenv("AI_PLATFORM_LOCK_STALE_SECONDS", "45")

    settings = Settings.from_env(tmp_path)

    assert settings.tasks_path == tmp_path / "configuration" / "tasks.json"
    assert settings.demo_repository == tmp_path / "sources" / "demo"
    assert settings.data_dir == tmp_path / "server" / "state"
    assert settings.workspace_root == tmp_path / "server" / "workspaces"
    assert settings.db_path == tmp_path / "server" / "state" / "custom.db"
    assert settings.max_attempts_per_tier == 3
    assert settings.lock_heartbeat_seconds == 7
    assert settings.lock_stale_seconds == 45


def test_config_rejects_stale_window_not_greater_than_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_PLATFORM_LOCK_HEARTBEAT_SECONDS", "10")
    monkeypatch.setenv("AI_PLATFORM_LOCK_STALE_SECONDS", "10")

    with pytest.raises(ValueError, match="must be greater"):
        Settings.from_env(tmp_path)


def test_doctor_is_read_only_and_reports_uninitialized_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        "ai_platform.doctor.inspect_claude_auth",
        lambda _settings: ClaudeAuthStatus(
            ClaudeAuthState.MISSING,
            "none",
            "Claude CLI reports logged out",
            Path("claude"),
        ),
    )

    checks = inspect_environment(settings)

    database = next(check for check in checks if check.name == "Platform DB")
    authentication = next(check for check in checks if check.name == "Claude auth")
    assert database.status is CheckStatus.WARN
    assert authentication.status is CheckStatus.FAIL
    assert not settings.db_path.exists()
    assert not settings.workspace_root.exists()


def test_claude_preflight_auth_failure_is_clean_and_makes_no_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ai_platform.executors.claude.inspect_claude_auth",
        lambda _settings: ClaudeAuthStatus(
            ClaudeAuthState.MISSING,
            "none",
            "Claude CLI reports logged out",
            Path("claude"),
        ),
    )

    with pytest.raises(AgentAuthenticationError, match="claude auth login") as error:
        ClaudeAgentExecutor(_settings(tmp_path)).preflight()

    assert "traceback" not in str(error.value).lower()


def test_service_preflights_before_creating_workspace_or_changing_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    definitions = load_tasks(settings.tasks_path)
    storage = SQLiteStorage(settings.db_path)
    storage.initialize()
    for task in definitions:
        storage.create_task(task)
    workspaces = LocalWorkspaceProvider(settings.workspace_root, settings.demo_repository)
    monkeypatch.setattr(
        "ai_platform.executors.claude.inspect_claude_auth",
        lambda _settings: ClaudeAuthStatus(
            ClaudeAuthState.MISSING,
            "none",
            "Claude CLI reports logged out",
            Path("claude"),
        ),
    )
    service = TaskSessionService(
        settings,
        definitions,
        storage,
        workspaces,
        ModelRouter.from_settings(settings),
        lambda: ClaudeAgentExecutor(settings),
    )

    with pytest.raises(TaskSessionError, match="claude auth login"):
        service.start(
            "DEMO-1",
            HumanIdentity(actor_id="alex", display_name="Alex"),
        )

    assert storage.get_task("DEMO-1").status is TaskStatus.READY
    assert storage.get_task("DEMO-1").active_execution is None
    assert not workspaces.exists("DEMO-1")
