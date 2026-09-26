"""Tests for task-owned copied Git workspaces."""

import stat
import subprocess
from pathlib import Path

import pytest

from ai_platform.workspace import LocalWorkspaceProvider, WorkspaceError, WorkspaceExistsError


def test_creates_clean_git_workspace_without_modifying_source(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "demo_repo"
    source_message = source / "app" / "messages.py"
    original = source_message.read_bytes()
    provider = LocalWorkspaceProvider(tmp_path / "workspaces", source)

    workspace = provider.create("DEMO-1")

    assert (workspace / ".git").is_dir()
    assert workspace.stat().st_mode & stat.S_ISGID
    assert (workspace / "app" / "messages.py").stat().st_mode & stat.S_IWGRP
    assert (workspace / "app" / "messages.py").read_bytes() == original
    assert provider.get_changed_files("DEMO-1") == []
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    assert log.stdout.strip() == "Baseline"
    assert source_message.read_bytes() == original

    with pytest.raises(WorkspaceExistsError):
        provider.create("DEMO-1")


def test_rejects_unsafe_task_id(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "demo_repo"
    provider = LocalWorkspaceProvider(tmp_path / "workspaces", source)

    with pytest.raises(ValueError, match="Unsafe task ID"):
        provider.get_path("../outside")


def test_destroy_refuses_source_repository_and_isolates_sibling_task(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    source = root / "SOURCE"
    source.mkdir(parents=True)
    (source / "keep.txt").write_text("source")
    sibling = root / "OTHER"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("other")
    provider = LocalWorkspaceProvider(root, source)

    with pytest.raises(WorkspaceError, match="source repository"):
        provider.destroy("SOURCE")
    assert (source / "keep.txt").read_text() == "source"
    assert (sibling / "keep.txt").read_text() == "other"


def test_workspace_symlink_cannot_redirect_reads_or_recursive_delete(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "demo_repo"
    root = tmp_path / "workspaces"
    root.mkdir()
    sibling = root / "TASK-B"
    sibling.mkdir()
    marker = sibling / "keep.txt"
    marker.write_text("task b", encoding="utf-8")
    (root / "TASK-A").symlink_to(sibling, target_is_directory=True)
    provider = LocalWorkspaceProvider(root, source)

    with pytest.raises(WorkspaceError, match="symlinked"):
        provider.get_diff("TASK-A")
    with pytest.raises(WorkspaceError, match="symlinked"):
        provider.destroy("TASK-A")
    assert marker.read_text(encoding="utf-8") == "task b"
