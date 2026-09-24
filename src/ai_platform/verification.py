"""Deterministic verification, including registered-repository baselines."""

import json
import os
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import monotonic

from pydantic import BaseModel, Field

from ai_platform.models import TaskDefinition, VerificationType
from ai_platform.workspace import LocalWorkspaceProvider


class VerificationResult(BaseModel):
    command: list[str]
    started_at: datetime
    finished_at: datetime
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    passed: bool
    timed_out: bool = False


class PytestRunResult(VerificationResult):
    failures: list[str] = Field(default_factory=list)
    passed_tests: int = 0
    collected_tests: int = 0
    infrastructure_error: str | None = None


class TaskSpecificVerification(BaseModel):
    targets: list[str]
    executed: bool
    passed: bool
    failures: list[str]
    passed_tests: int
    infrastructure_error: str | None = None


class BroadRegressionVerification(BaseModel):
    baseline_identity: str
    baseline_failures: list[str]
    current_failures: list[str]
    pre_existing_failures: list[str]
    fixed_failures: list[str]
    new_failures: list[str]
    passed: bool
    baseline_cached: bool
    infrastructure_error: str | None = None


class BaselineAwareVerificationResult(BaseModel):
    task_specific: TaskSpecificVerification
    broad_regression: BroadRegressionVerification
    passed: bool
    warnings: list[str]
    blocking_failures: list[str]
    current_run: PytestRunResult

    def correction_context(self) -> str:
        status = "PASS" if self.task_specific.passed else "FAIL"
        lines = [f"Task-specific tests: {status}"]
        lines.extend(f"  {item}" for item in self.task_specific.failures)
        lines.append(f"New regressions: {len(self.broad_regression.new_failures)}")
        lines.extend(f"  {item}" for item in self.broad_regression.new_failures)
        if self.broad_regression.infrastructure_error:
            lines.append(
                f"Verification infrastructure: {self.broad_regression.infrastructure_error}"
            )
        lines.append(
            "Pre-existing baseline failures (non-blocking): "
            f"{len(self.broad_regression.pre_existing_failures)}"
        )
        lines.extend(
            f"  {item}" for item in self.broad_regression.pre_existing_failures
        )
        return "\n".join(lines)


class BaselineContext(BaseModel):
    repository_id: str
    source_repository: Path
    source_commit: str


class BaselineCache:
    """Process-local immutable-SHA cache with serialized creation per key."""

    def __init__(self) -> None:
        self._values: dict[str, PytestRunResult] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get_or_compute(self, key: str, operation) -> tuple[PytestRunResult, bool]:
        with self._guard:
            if key in self._values:
                return self._values[key], True
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            with self._guard:
                if key in self._values:
                    return self._values[key], True
            value = operation()
            if value.infrastructure_error:
                return value, False
            with self._guard:
                self._values[key] = value
                self._locks.pop(key, None)
            return value, False


BASELINE_CACHE = BaselineCache()


def build_verification_command(
    task: TaskDefinition, targets: list[str] | None = None, *, reporter: bool = False
) -> list[str]:
    if task.verification.type is not VerificationType.PYTEST:
        raise ValueError(f"Unsupported verification type: {task.verification.type}")
    plugins = ["-p", "no:cacheprovider"]
    if reporter:
        plugins += ["-p", "ai_platform.pytest_reporter"]
    return [sys.executable, "-m", "pytest", *plugins, *(targets or task.verification.targets)]


def verify_task(task: TaskDefinition, workspace: Path, timeout_seconds: int) -> VerificationResult:
    """Preserve explicit legacy verification behavior."""

    result = _run_pytest(task, workspace, task.verification.targets, timeout_seconds)
    excluded = {"failures", "passed_tests", "collected_tests", "infrastructure_error"}
    return VerificationResult(**result.model_dump(exclude=excluded))


def verify_registered_task(
    task: TaskDefinition,
    workspace: Path,
    timeout_seconds: int,
    baseline: BaselineContext,
    changed_paths: list[str],
    *,
    cache: BaselineCache = BASELINE_CACHE,
) -> BaselineAwareVerificationResult:
    """Run task-specific tests and broad failure delta against a clean export."""

    broad_targets = task.verification.targets
    explicit_narrow = broad_targets != ["tests"]
    task_targets = (
        broad_targets
        if explicit_narrow
        else sorted(
            path
            for path in changed_paths
            if _is_test_file(path) and (workspace / path).is_file()
        )
    )
    task_run = (
        _run_pytest(task, workspace, task_targets, timeout_seconds)
        if task_targets
        else None
    )
    identity = _baseline_identity(baseline, task)

    def acquire_baseline() -> PytestRunResult:
        with tempfile.TemporaryDirectory(prefix="ai-platform-baseline-") as directory:
            provider = LocalWorkspaceProvider(
                Path(directory) / "workspaces", baseline.source_repository
            )
            clean = provider.create(
                "baseline", baseline.source_repository, baseline.source_commit
            )
            return _run_pytest(task, clean, broad_targets, timeout_seconds)

    baseline_run, cached = cache.get_or_compute(identity, acquire_baseline)
    current_run = (
        task_run
        if explicit_narrow and task_run is not None
        else _run_pytest(task, workspace, broad_targets, timeout_seconds)
    )
    baseline_failures = set(baseline_run.failures)
    current_failures = set(current_run.failures)
    pre_existing = sorted(baseline_failures & current_failures)
    fixed = sorted(baseline_failures - current_failures)
    new = sorted(current_failures - baseline_failures)
    infrastructure = baseline_run.infrastructure_error or current_run.infrastructure_error
    broad_passed = infrastructure is None and not new
    task_passed = task_run is None or (
        task_run.infrastructure_error is None and task_run.passed
    )
    task_specific = TaskSpecificVerification(
        targets=task_targets,
        executed=task_run is not None,
        passed=task_passed,
        failures=task_run.failures if task_run else [],
        passed_tests=task_run.passed_tests if task_run else 0,
        infrastructure_error=task_run.infrastructure_error if task_run else None,
    )
    broad = BroadRegressionVerification(
        baseline_identity=identity,
        baseline_failures=sorted(baseline_failures),
        current_failures=sorted(current_failures),
        pre_existing_failures=pre_existing,
        fixed_failures=fixed,
        new_failures=new,
        passed=broad_passed,
        baseline_cached=cached,
        infrastructure_error=infrastructure,
    )
    blocking = list(task_specific.failures) + new
    if task_specific.infrastructure_error:
        blocking.append(task_specific.infrastructure_error)
    if infrastructure:
        blocking.append(infrastructure)
    return BaselineAwareVerificationResult(
        task_specific=task_specific,
        broad_regression=broad,
        passed=task_passed and broad_passed,
        warnings=[f"Pre-existing baseline failure: {item}" for item in pre_existing],
        blocking_failures=blocking,
        current_run=current_run,
    )


def _run_pytest(
    task: TaskDefinition, workspace: Path, targets: list[str], timeout_seconds: int
) -> PytestRunResult:
    workspace = workspace.resolve()
    for target in targets:
        resolved = (workspace / target).resolve()
        if not resolved.is_relative_to(workspace):
            raise ValueError(f"Verification target escapes workspace: {target}")
    command = build_verification_command(task, targets, reporter=True)
    started_at = datetime.now(UTC)
    started_clock = monotonic()
    with tempfile.TemporaryDirectory(prefix="ai-platform-pytest-report-") as directory:
        report_path = Path(directory) / "report.json"
        package_root = str(Path(__file__).resolve().parents[1])
        existing_pythonpath = os.environ.get("PYTHONPATH", "")
        environment = {
            **os.environ,
            "AI_PLATFORM_PYTEST_REPORT": str(report_path),
            "PYTHONPATH": os.pathsep.join(
                part for part in (package_root, existing_pythonpath) if part
            ),
        }
        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                shell=False,
                env=environment,
            )
            report = _load_report(report_path)
            infrastructure = _infrastructure_error(completed.returncode, report)
            return PytestRunResult(
                command=command,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                duration_seconds=monotonic() - started_clock,
                passed=completed.returncode == 0 and infrastructure is None,
                failures=report.get("failures", []),
                passed_tests=report.get("passed", 0),
                collected_tests=report.get("collected", 0),
                infrastructure_error=infrastructure,
            )
        except subprocess.TimeoutExpired as error:
            message = f"Verification timed out after {timeout_seconds} seconds"
            return PytestRunResult(
                command=command,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                exit_code=-1,
                stdout=error.stdout if isinstance(error.stdout, str) else "",
                stderr=error.stderr if isinstance(error.stderr, str) else message,
                duration_seconds=monotonic() - started_clock,
                passed=False,
                timed_out=True,
                infrastructure_error=message,
            )


def _load_report(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _infrastructure_error(exit_code: int, report: dict) -> str | None:
    if not report:
        return "pytest did not produce a structured result"
    if exit_code == 2 and report.get("failures"):
        # Pytest uses 2 for collection interruption as well as user interruption.
        # A reporter-captured collection identity makes the former deterministic.
        return None
    if exit_code not in {0, 1}:
        return f"pytest could not complete safely (exit code {exit_code})"
    if exit_code == 1 and not report.get("failures"):
        return "pytest failed without a normalized test identity"
    return None


def _is_test_file(path: str) -> bool:
    candidate = Path(path)
    return (
        len(candidate.parts) > 1
        and candidate.parts[0] == "tests"
        and candidate.suffix == ".py"
        and (candidate.name.startswith("test_") or candidate.name.endswith("_test.py"))
    )


def _baseline_identity(context: BaselineContext, task: TaskDefinition) -> str:
    payload = {
        "repository_id": context.repository_id,
        "source_commit": context.source_commit,
        "verification": task.verification.model_dump(mode="json"),
        "python": sys.executable,
    }
    return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
