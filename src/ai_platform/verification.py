"""Deterministic, platform-owned task verification."""

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from pydantic import BaseModel

from ai_platform.models import TaskDefinition, VerificationType


class VerificationResult(BaseModel):
    """Captured result of a bounded verification subprocess."""

    command: list[str]
    started_at: datetime
    finished_at: datetime
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    passed: bool
    timed_out: bool = False


def build_verification_command(task: TaskDefinition) -> list[str]:
    """Build a safe argv list from validated structured task configuration."""

    if task.verification.type is not VerificationType.PYTEST:
        raise ValueError(f"Unsupported verification type: {task.verification.type}")
    return [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        *task.verification.targets,
    ]


def verify_task(task: TaskDefinition, workspace: Path, timeout_seconds: int) -> VerificationResult:
    """Run the task-specific pytest target without invoking a shell."""

    workspace = workspace.resolve()
    for target in task.verification.targets:
        resolved_target = (workspace / target).resolve()
        if not resolved_target.is_relative_to(workspace):
            raise ValueError(f"Verification target escapes workspace: {target}")

    command = build_verification_command(task)
    started_at = datetime.now(UTC)
    started_clock = monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
        )
        finished_at = datetime.now(UTC)
        return VerificationResult(
            command=command,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=monotonic() - started_clock,
            passed=completed.returncode == 0,
        )
    except subprocess.TimeoutExpired as error:
        finished_at = datetime.now(UTC)
        stdout = error.stdout if isinstance(error.stdout, str) else ""
        stderr = error.stderr if isinstance(error.stderr, str) else ""
        return VerificationResult(
            command=command,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=-1,
            stdout=stdout,
            stderr=stderr or f"Verification timed out after {timeout_seconds} seconds",
            duration_seconds=monotonic() - started_clock,
            passed=False,
            timed_out=True,
        )
