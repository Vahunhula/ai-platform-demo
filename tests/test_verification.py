"""Tests for deterministic platform-owned verification."""

from pathlib import Path

from ai_platform.task_loader import get_task, load_tasks
from ai_platform.verification import build_verification_command, verify_task


def test_verification_disables_cross_user_pytest_cache(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    task = get_task(load_tasks(root / "tests" / "fixtures" / "demo_tasks.json"), "DEMO-1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_messages.py").write_text("def test_ok():\n    assert True\n")

    command = build_verification_command(task)
    result = verify_task(task, workspace, timeout_seconds=20)

    assert command[3:5] == ["-p", "no:cacheprovider"]
    assert result.passed is True
    assert not (workspace / ".pytest_cache").exists()
