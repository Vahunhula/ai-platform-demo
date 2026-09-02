"""CLI inspection tests that never invoke the real executor."""

from pathlib import Path

from typer.testing import CliRunner

from ai_platform.cli import app
from ai_platform.workspace import LocalWorkspaceProvider


def test_diff_command_reads_task_workspace(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    workspace_root = tmp_path / "workspaces"
    provider = LocalWorkspaceProvider(workspace_root, root / "demo_repo")
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
