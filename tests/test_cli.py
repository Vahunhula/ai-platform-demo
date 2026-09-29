"""CLI inspection tests that never invoke the real executor."""

from pathlib import Path

from typer.testing import CliRunner

from ai_platform.cli import app
from ai_platform.events import EventType
from ai_platform.storage import SQLiteStorage
from ai_platform.workspace import LocalWorkspaceProvider


def test_diff_command_reads_task_workspace(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    workspace_root = tmp_path / "workspaces"
    provider = LocalWorkspaceProvider(workspace_root, root / "tests" / "fixtures" / "demo_repo")
    workspace = provider.create("DEMO-1")
    message_file = workspace / "app" / "messages.py"
    message_file.write_text(
        message_file.read_text(encoding="utf-8").replace("Welocme", "Welcome"),
        encoding="utf-8",
    )
    env = {
        "AI_PLATFORM_DATA_DIR": str(tmp_path / "data"),
        "AI_PLATFORM_WORKSPACE_ROOT": str(workspace_root),
        "AI_PLATFORM_DB_PATH": str(tmp_path / "data" / "platform.db"),
        "AI_PLATFORM_CHECKPOINT_DB_PATH": str(tmp_path / "data" / "checkpoints.db"),
    }

    result = CliRunner().invoke(app, ["diff", "DEMO-1"], env=env)

    assert result.exit_code == 0
    assert "app/messages.py" in result.stdout
    assert "Welocme" in result.stdout
    assert "Welcome" in result.stdout


def test_generic_platform_command_uses_registry_service(tmp_path: Path) -> None:
    env = {
        "AI_PLATFORM_DATA_DIR": str(tmp_path / "data"),
        "AI_PLATFORM_WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        "AI_PLATFORM_DB_PATH": str(tmp_path / "data" / "platform.db"),
        "AI_PLATFORM_CHECKPOINT_DB_PATH": str(tmp_path / "data" / "checkpoints.db"),
        "AI_PLATFORM_USER": "cli-command-user",
    }

    result = CliRunner().invoke(app, ["command", "DEMO-1", "/status"], env=env)

    assert result.exit_code == 0
    assert "/status" in result.stdout
    assert "IMPLEMENTATION" in result.stdout
    storage = SQLiteStorage(tmp_path / "data" / "platform.db")
    events = storage.get_events("DEMO-1")
    assert [event.event_type for event in events][-2:] == [
        EventType.COMMAND_INVOKED,
        EventType.COMMAND_SUCCEEDED,
    ]
    assert events[-2].actor_id == "cli-command-user"


def test_generic_claude_command_uses_allowlisted_service(tmp_path: Path) -> None:
    env = {
        "AI_PLATFORM_DATA_DIR": str(tmp_path / "data"),
        "AI_PLATFORM_WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        "AI_PLATFORM_DB_PATH": str(tmp_path / "data" / "platform.db"),
        "AI_PLATFORM_CHECKPOINT_DB_PATH": str(tmp_path / "data" / "checkpoints.db"),
        "AI_PLATFORM_USER": "cli-claude-user",
    }

    result = CliRunner().invoke(app, ["claude-command", "DEMO-1", "claude/status"], env=env)

    assert result.exit_code == 0
    assert "claude/status" in result.stdout
    assert "workspace_write" in result.stdout
    storage = SQLiteStorage(tmp_path / "data" / "platform.db")
    events = storage.get_events("DEMO-1")
    assert events[-2].metadata["namespace"] == "claude"
    assert events[-2].actor_id == "cli-claude-user"
