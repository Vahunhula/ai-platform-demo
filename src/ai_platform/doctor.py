"""Read-only, cross-platform environment diagnostics."""

import importlib.metadata
import importlib.util
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ai_platform.claude_auth import ClaudeAuthState, inspect_claude_auth
from ai_platform.config import Settings
from ai_platform.task_loader import load_tasks


class CheckStatus(StrEnum):
    """Doctor severity with predictable CLI exit behavior."""

    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One sanitized environment diagnostic."""

    name: str
    status: CheckStatus
    detail: str


def inspect_environment(settings: Settings) -> list[DoctorCheck]:
    """Inspect prerequisites without initializing or changing task state."""

    checks = [
        _python_check(),
        _command_check("Git", "git", ["--version"]),
        DoctorCheck("SQLite", CheckStatus.OK, sqlite3.sqlite_version),
        _sdk_check(),
    ]
    auth = inspect_claude_auth(settings)
    checks.append(
        DoctorCheck(
            "Claude CLI",
            (
                CheckStatus.OK
                if auth.cli_path
                else (
                    CheckStatus.WARN
                    if auth.state is ClaudeAuthState.CONFIGURED
                    else CheckStatus.FAIL
                )
            ),
            str(auth.cli_path) if auth.cli_path else "not found on PATH",
        )
    )
    auth_status = {
        ClaudeAuthState.AUTHENTICATED: CheckStatus.OK,
        ClaudeAuthState.CONFIGURED: CheckStatus.WARN,
        ClaudeAuthState.MISSING: CheckStatus.FAIL,
        ClaudeAuthState.UNKNOWN: CheckStatus.WARN,
    }[auth.state]
    checks.append(DoctorCheck("Claude auth", auth_status, f"{auth.method}: {auth.detail}"))
    checks.append(
        DoctorCheck(
            "Model access",
            CheckStatus.WARN,
            "not checked; validation would require a real provider request",
        )
    )
    checks.extend(
        [
            _repository_check(settings.demo_repository),
            _task_file_check(settings.tasks_path),
            _writable_path_check("Data directory", settings.data_dir),
            _writable_path_check("Workspace root", settings.workspace_root),
            _database_check(settings.db_path),
            _git_worktree_check(settings.project_root),
            DoctorCheck(
                "OS",
                CheckStatus.OK,
                f"{platform.system()} {platform.release()} ({os.name})",
            ),
        ]
    )
    return checks


def _python_check() -> DoctorCheck:
    version = platform.python_version()
    status = CheckStatus.OK if sys.version_info >= (3, 12) else CheckStatus.FAIL
    return DoctorCheck("Python", status, f"{version} at {sys.executable}")


def _command_check(name: str, command: str, arguments: list[str]) -> DoctorCheck:
    executable = shutil.which(command)
    if not executable:
        return DoctorCheck(name, CheckStatus.FAIL, "not found on PATH")
    try:
        result = subprocess.run(
            [executable, *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return DoctorCheck(name, CheckStatus.FAIL, type(error).__name__)
    detail = (result.stdout or result.stderr).strip().splitlines()
    rendered = detail[0] if detail else f"exit code {result.returncode}"
    status = CheckStatus.OK if result.returncode == 0 else CheckStatus.FAIL
    return DoctorCheck(name, status, f"{rendered} at {executable}")


def _sdk_check() -> DoctorCheck:
    if importlib.util.find_spec("claude_agent_sdk") is None:
        return DoctorCheck("Claude Agent SDK", CheckStatus.FAIL, "not installed")
    try:
        version = importlib.metadata.version("claude-agent-sdk")
    except importlib.metadata.PackageNotFoundError:
        return DoctorCheck("Claude Agent SDK", CheckStatus.FAIL, "version unavailable")
    return DoctorCheck("Claude Agent SDK", CheckStatus.OK, version)


def _repository_check(path: Path) -> DoctorCheck:
    if not path.is_dir():
        return DoctorCheck("Demo repository", CheckStatus.FAIL, f"missing: {path}")
    if not any(path.iterdir()):
        return DoctorCheck("Demo repository", CheckStatus.FAIL, f"empty: {path}")
    return DoctorCheck("Demo repository", CheckStatus.OK, f"source directory: {path}")


def _task_file_check(path: Path) -> DoctorCheck:
    if not path.is_file():
        return DoctorCheck("Task file", CheckStatus.FAIL, f"missing: {path}")
    try:
        count = len(load_tasks(path))
    except Exception as error:
        return DoctorCheck("Task file", CheckStatus.FAIL, str(error)[:500])
    return DoctorCheck("Task file", CheckStatus.OK, f"{path} ({count} tasks)")


def _writable_path_check(name: str, path: Path) -> DoctorCheck:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.exists():
        return DoctorCheck(name, CheckStatus.FAIL, f"no existing parent for {path}")
    if not candidate.is_dir():
        candidate = candidate.parent
    writable = os.access(candidate, os.W_OK)
    detail = str(path) if path.exists() else f"{path} (will be created under {candidate})"
    return DoctorCheck(name, CheckStatus.OK if writable else CheckStatus.FAIL, detail)


def _database_check(path: Path) -> DoctorCheck:
    if not path.exists():
        return DoctorCheck("Platform DB", CheckStatus.WARN, f"not initialized: {path}")
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as error:
        return DoctorCheck("Platform DB", CheckStatus.FAIL, str(error))
    healthy = bool(result and result[0] == "ok")
    return DoctorCheck(
        "Platform DB",
        CheckStatus.OK if healthy else CheckStatus.FAIL,
        str(path) if healthy else str(result[0] if result else "quick_check returned no result"),
    )


def _git_worktree_check(path: Path) -> DoctorCheck:
    executable = shutil.which("git")
    if not executable:
        return DoctorCheck("Git working tree", CheckStatus.FAIL, "Git is unavailable")
    try:
        result = subprocess.run(
            [executable, "-C", str(path), "status", "--short"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return DoctorCheck("Git working tree", CheckStatus.FAIL, type(error).__name__)
    if result.returncode != 0:
        return DoctorCheck(
            "Git working tree", CheckStatus.FAIL, (result.stderr or "not a repository").strip()
        )
    changes = len(result.stdout.splitlines())
    detail = "clean" if changes == 0 else f"{changes} uncommitted path(s)"
    return DoctorCheck(
        "Git working tree",
        CheckStatus.OK if changes == 0 else CheckStatus.WARN,
        detail,
    )
