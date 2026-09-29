"""Phase 2.2 baseline-aware verification regression and isolation tests."""

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ai_platform.models import (
    TaskDefinition,
    TaskDifficulty,
    VerificationConfig,
    VerificationType,
)
from ai_platform.verification import (
    BaselineCache,
    BaselineContext,
    verify_registered_task,
)
from ai_platform.workspace import LocalWorkspaceProvider


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _repository(root: Path, *, marker: str = "base") -> tuple[Path, str]:
    repository = root / marker
    (repository / "app").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "app" / "values.py").write_text(
        "discount = 5\nmessage = 'bad'\nname = 'bad'\n", encoding="utf-8"
    )
    failures = {
        "discounts": (
            "from app.values import discount\ndef test_discount(): assert discount == 10\n"
        ),
        "messages": (
            "from app.values import message\ndef test_message(): assert message == 'good'\n"
        ),
        "users": "from app.values import name\ndef test_name(): assert name == 'good'\n",
    }
    for name, content in failures.items():
        (repository / "tests" / f"test_{name}.py").write_text(content, encoding="utf-8")
    (repository / "tests" / "test_existing.py").write_text(
        "def test_existing(): assert True\n", encoding="utf-8"
    )
    (repository / "app" / "__init__.py").write_text("", encoding="utf-8")
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "baseline")
    return repository, _git(repository, "rev-parse", "HEAD")


def _task(targets: list[str] | None = None) -> TaskDefinition:
    return TaskDefinition(
        id="HOUSE-1",
        title="House",
        description="Draw a house",
        difficulty=TaskDifficulty.MEDIUM,
        acceptance_criteria=["House tests pass"],
        verification=VerificationConfig(type=VerificationType.PYTEST, targets=targets or ["tests"]),
    )


def _workspace(root: Path, repository: Path, task_id: str = "HOUSE-1") -> Path:
    return LocalWorkspaceProvider(root / "workspaces", repository).create(
        task_id, repository, "main"
    )


def _context(repository: Path, commit: str, identity: str = "repo-house") -> BaselineContext:
    return BaselineContext(
        repository_id=identity,
        source_repository=repository,
        source_commit=commit,
    )


def _add_house(workspace: Path, *, passing: bool = True) -> None:
    (workspace / "app" / "house.py").write_text("def house(): return '#'\n", encoding="utf-8")
    assertion = "house() == '#'" if passing else "house() == 'wrong'"
    (workspace / "tests" / "test_house.py").write_text(
        "from app.house import house\n"
        f"def test_house(): assert {assertion}\n"
        "def test_roof(): assert house().startswith('#')\n"
        "def test_wall(): assert '#' in house()\n"
        "def test_door(): assert len(house()) == 1\n",
        encoding="utf-8",
    )


def test_house_unchanged_baseline_failures_pass(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    workspace = _workspace(tmp_path, repository)
    _add_house(workspace)
    result = verify_registered_task(
        _task(),
        workspace,
        20,
        _context(repository, commit),
        ["app/house.py", "tests/test_house.py"],
        cache=BaselineCache(),
    )
    assert result.passed
    assert result.task_specific.targets == ["tests/test_house.py"]
    assert result.task_specific.passed_tests == 4
    assert len(result.broad_regression.pre_existing_failures) == 3
    assert result.broad_regression.new_failures == []


def test_new_broad_regression_blocks_even_when_house_passes(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    workspace = _workspace(tmp_path, repository)
    _add_house(workspace)
    (workspace / "tests" / "test_regression.py").write_text(
        "def test_new_regression(): assert False\n", encoding="utf-8"
    )
    result = verify_registered_task(
        _task(),
        workspace,
        20,
        _context(repository, commit),
        ["tests/test_house.py", "tests/test_regression.py"],
        cache=BaselineCache(),
    )
    assert not result.passed
    assert result.task_specific.failures == ["tests/test_regression.py::test_new_regression"]
    assert result.broad_regression.new_failures == ["tests/test_regression.py::test_new_regression"]


def test_task_specific_failure_blocks(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    workspace = _workspace(tmp_path, repository)
    _add_house(workspace, passing=False)
    result = verify_registered_task(
        _task(),
        workspace,
        20,
        _context(repository, commit),
        ["tests/test_house.py"],
        cache=BaselineCache(),
    )
    assert not result.task_specific.passed
    assert not result.passed


def test_fixed_baseline_failure_is_informational(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    workspace = _workspace(tmp_path, repository)
    values = workspace / "app" / "values.py"
    values.write_text(values.read_text().replace("discount = 5", "discount = 10"))
    result = verify_registered_task(
        _task(),
        workspace,
        20,
        _context(repository, commit),
        ["app/values.py"],
        cache=BaselineCache(),
    )
    assert result.passed
    assert result.task_specific.executed is False
    assert result.broad_regression.fixed_failures == ["tests/test_discounts.py::test_discount"]


def test_no_changed_tests_uses_broad_delta(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    workspace = _workspace(tmp_path, repository)
    result = verify_registered_task(
        _task(), workspace, 20, _context(repository, commit), [], cache=BaselineCache()
    )
    assert result.passed
    assert result.task_specific.targets == []
    assert result.task_specific.executed is False


def test_explicit_narrow_target_is_preserved(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    workspace = _workspace(tmp_path, repository)
    result = verify_registered_task(
        _task(["tests/test_existing.py"]),
        workspace,
        20,
        _context(repository, commit),
        [],
        cache=BaselineCache(),
    )
    assert result.passed
    assert result.task_specific.targets == ["tests/test_existing.py"]


def test_baseline_command_failure_fails_closed(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    workspace = _workspace(tmp_path, repository)
    result = verify_registered_task(
        _task(["tests/missing.py"]),
        workspace,
        20,
        _context(repository, commit),
        [],
        cache=BaselineCache(),
    )
    assert not result.passed
    assert result.broad_regression.infrastructure_error


def test_concurrent_tasks_share_only_same_immutable_baseline(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    workspace_a = _workspace(tmp_path / "a", repository, "TASK-A")
    workspace_b = _workspace(tmp_path / "b", repository, "TASK-B")
    _add_house(workspace_a)
    _add_house(workspace_b)
    cache = BaselineCache()

    def run(workspace: Path):
        return verify_registered_task(
            _task(),
            workspace,
            20,
            _context(repository, commit),
            ["tests/test_house.py"],
            cache=cache,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [workspace_a, workspace_b]))
    assert all(result.passed for result in results)
    assert sorted(result.broad_regression.baseline_cached for result in results) == [False, True]
    assert all(len(result.broad_regression.pre_existing_failures) == 3 for result in results)


def test_baseline_identity_does_not_cross_repositories(tmp_path: Path) -> None:
    repository_a, commit_a = _repository(tmp_path / "one", marker="repo")
    repository_b, commit_b = _repository(tmp_path / "two", marker="repo")
    workspace_a = _workspace(tmp_path / "wa", repository_a)
    workspace_b = _workspace(tmp_path / "wb", repository_b)
    cache = BaselineCache()
    result_a = verify_registered_task(
        _task(), workspace_a, 20, _context(repository_a, commit_a, "repo-a"), [], cache=cache
    )
    result_b = verify_registered_task(
        _task(), workspace_b, 20, _context(repository_b, commit_b, "repo-b"), [], cache=cache
    )
    assert (
        result_a.broad_regression.baseline_identity != result_b.broad_regression.baseline_identity
    )
    assert not result_a.broad_regression.baseline_cached
    assert not result_b.broad_regression.baseline_cached
