"""Demo 2.5.2 task-owned test isolation, synthesis, upload, and cleanup regressions."""

import dataclasses
import sqlite3
import subprocess
from pathlib import Path

import pytest

from ai_platform.api.app import create_app
from ai_platform.application import create_application_context
from ai_platform.auth import AuthenticatedUser, Role
from ai_platform.executors.base import ExecutionRequest, ExecutionResult
from ai_platform.identity import HumanIdentity
from ai_platform.models import TaskDisposition, TaskOrigin, TaskStatus
from ai_platform.models import TestFileSource as FileSource
from ai_platform.removal import TaskRemovalService
from ai_platform.task_creation import CreateTaskCommand, TaskCreationError
from ai_platform.task_tests import UploadedTestInput
from ai_platform.workflow import WorkflowPhase
from ai_platform.workspace import LocalWorkspaceProvider
from tests.fakes import FakeAgentExecutor
from tests.test_messaging import _settings


def _repository(root: Path) -> tuple[Path, str]:
    repository = root / "repository"
    (repository / "app").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repository / "tests" / "test_repository.py").write_text(
        "def test_repository_baseline():\n    assert True\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.test"], cwd=repository, check=True
    )
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "clean baseline"], cwd=repository, check=True, capture_output=True
    )
    return repository, "main"


def _setup(tmp_path: Path, executor=None):
    tasks = tmp_path / "tasks.json"
    tasks.write_text("[]\n", encoding="utf-8")
    settings = dataclasses.replace(_settings(tmp_path), tasks_path=tasks)
    context = create_application_context(
        settings, executor_factory=lambda: executor or FakeAgentExecutor()
    )
    app = create_app(context)
    user = app.state.auth.add_user("alex", "Alex", Role.DEVELOPER)
    repository, branch = _repository(tmp_path)
    registered = context.repositories.register_local("clean", "Clean", repository, branch)
    actor = AuthenticatedUser(user.user_id, user.username, user.display_name, user.role)
    return context, app, registered, actor, repository


def _command(repository_id: str, user_id: str, **overrides):
    values = dict(
        title="Task owned tests",
        description="Make the requested behavior work.",
        repository_id=repository_id,
        base_branch="main",
        assignee_user_id=user_id,
        jira_key=None,
    )
    values.update(overrides)
    return CreateTaskCommand(**values)


def test_uploaded_test_is_task_owned_isolated_and_deleted(tmp_path: Path) -> None:
    context, app, repository, actor, source = _setup(tmp_path)
    test_a = UploadedTestInput("test_example.py", "def test_a():\n    assert True\n")
    task_a = app.state.task_creation.create(
        _command(repository.id, actor.user_id, uploaded_test_files=(test_a,)), actor
    )
    task_b = app.state.task_creation.create(_command(repository.id, actor.user_id), actor)
    task_c = app.state.task_creation.create(_command(repository.id, actor.user_id), actor)
    relative = Path(".ai-platform/tests/uploaded/test_example.py")
    assert (context.workspaces.get_path(task_a.task_id) / relative).is_file()
    assert not (context.workspaces.get_path(task_b.task_id) / relative).exists()
    assert not (context.workspaces.get_path(task_c.task_id) / relative).exists()
    assert not (source / relative).exists()
    stored = context.storage.list_task_test_files(task_a.task_id)
    assert len(stored) == 1 and stored[0].source is FileSource.UPLOADED
    context.sessions.start(
        task_a.task_id,
        HumanIdentity(actor_id=actor.user_id, display_name=actor.display_name),
    )
    verification = [
        event
        for event in context.storage.get_events(task_a.task_id)
        if event.event_type.value == "TEST_PASSED"
    ][-1]
    assert verification.metadata["task_acceptance_status"] == "PASS"

    with context.storage.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE tasks SET status = ?, disposition = ? WHERE task_id = ?",
            (TaskStatus.COMPLETED.value, TaskDisposition.DEFERRED.value, task_a.task_id),
        )
    removal = TaskRemovalService(
        context.storage, context.workspaces, context.settings.checkpoint_db_path
    )
    removal.remove(task_a.task_id, actor)
    assert not context.workspaces.get_path(task_a.task_id).exists()
    assert context.storage.get_task(task_a.task_id) is None
    assert context.workspaces.get_path(task_b.task_id).is_dir()


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("../../evil.py", ""),
        ("/tmp/evil.py", ""),
        ("link/evil.py", ""),
        ("test.exe", ""),
        ("test_big.py", "x" * (256 * 1024 + 1)),
    ],
    ids=["traversal", "absolute", "nested", "unsupported", "oversized"],
)
def test_uploaded_test_rejects_unsafe_input(tmp_path: Path, filename: str, content: str) -> None:
    _context, app, repository, actor, _source = _setup(tmp_path)
    with pytest.raises(TaskCreationError):
        app.state.task_creation.create(
            _command(
                repository.id,
                actor.user_id,
                uploaded_test_files=(UploadedTestInput(filename, content),),
            ),
            actor,
        )


class SynthesisExecutor(FakeAgentExecutor):
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if request.test_synthesis_stories:
            output = request.workspace_path / ".ai-platform/tests/generated/test_even.py"
            output.parent.mkdir(parents=True)
            output.write_text(
                "from app.even import is_even\n\n"
                "def test_even_values():\n"
                "    assert is_even(2) is True\n"
                "    assert is_even(3) is False\n"
                "    assert is_even(0) is True\n",
                encoding="utf-8",
            )
            return ExecutionResult(
                succeeded=True,
                summary="Generated acceptance coverage",
                structured_output={
                    "status": "GENERATED",
                    "message": "Generated from all three stories",
                    "requirement_mapping": {
                        "2 should be even": ["test_even_values"],
                        "3 should not be even": ["test_even_values"],
                        "0 should be even": ["test_even_values"],
                    },
                },
            )
        if request.phase is WorkflowPhase.IMPLEMENTATION:
            (request.workspace_path / "app/even.py").write_text(
                "def is_even(value: int) -> bool:\n    return value % 2 == 0\n",
                encoding="utf-8",
            )
        return super().execute(request)


class NeedsHumanSynthesisExecutor(FakeAgentExecutor):
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if request.test_synthesis_stories:
            return ExecutionResult(
                succeeded=True,
                summary="Fixture details are missing",
                structured_output={
                    "status": "NEEDS_HUMAN",
                    "message": "Which fixture supplies negative values?",
                    "requirement_mapping": {},
                },
            )
        return super().execute(request)


def test_human_story_synthesizes_and_verifies_only_in_own_task(tmp_path: Path) -> None:
    executor = SynthesisExecutor()
    context, app, repository, actor, source = _setup(tmp_path, executor)
    stories = "- 2 should be even.\n- 3 should not be even.\n- 0 should be even."
    task = app.state.task_creation.create(
        _command(repository.id, actor.user_id, acceptance_test_stories=stories), actor
    )
    other = app.state.task_creation.create(_command(repository.id, actor.user_id), actor)
    context.sessions.start(
        task.task_id,
        HumanIdentity(actor_id=actor.user_id, display_name=actor.display_name),
    )

    specification = context.storage.get_test_specification(task.task_id)
    assert specification is not None and specification.original_text == stories
    generated = context.storage.list_task_test_files(task.task_id)
    assert len(generated) == 1 and generated[0].source is FileSource.GENERATED
    path = Path(generated[0].relative_path)
    assert (context.workspaces.get_path(task.task_id) / path).is_file()
    assert not (context.workspaces.get_path(other.task_id) / path).exists()
    assert not (source / path).exists()
    record = context.storage.get_task(task.task_id)
    assert record is not None and record.verification_status.value == "passed"
    verification = [
        event
        for event in context.storage.get_events(task.task_id)
        if event.event_type.value == "TEST_PASSED"
    ][-1]
    assert verification.metadata["task_acceptance_status"] == "PASS"
    assert verification.metadata["task_acceptance_targets"] == [generated[0].relative_path]


def test_human_story_source_text_is_persisted_exactly(tmp_path: Path) -> None:
    context, app, repository, actor, _source = _setup(tmp_path)
    stories = "  - Quantity 10 should be accepted\n- Preserve this final line.  \n"
    task = app.state.task_creation.create(
        _command(repository.id, actor.user_id, acceptance_test_stories=stories), actor
    )

    specification = context.storage.get_test_specification(task.task_id)
    assert specification is not None
    assert specification.original_text == stories


def test_unclear_story_stops_with_needs_human_reason(tmp_path: Path) -> None:
    context, app, repository, actor, _source = _setup(tmp_path, NeedsHumanSynthesisExecutor())
    task = app.state.task_creation.create(
        _command(
            repository.id,
            actor.user_id,
            acceptance_test_stories="Negative values are rejected.",
        ),
        actor,
    )
    context.sessions.start(
        task.task_id,
        HumanIdentity(actor_id=actor.user_id, display_name=actor.display_name),
    )
    specification = context.storage.get_test_specification(task.task_id)
    assert specification is not None
    assert specification.generation_status.value == "NEEDS_HUMAN"
    assert specification.generation_message == "Which fixture supplies negative values?"
    assert context.storage.list_task_test_files(task.task_id) == []
    assert context.storage.get_task(task.task_id).status is TaskStatus.WAITING_FOR_HUMAN


def test_disposable_system_task_cleanup_does_not_weaken_user_task(tmp_path: Path) -> None:
    context, app, repository, actor, _source = _setup(tmp_path)
    disposable = app.state.task_creation.create(
        _command(
            repository.id,
            actor.user_id,
            origin=TaskOrigin.SYSTEM_TEST,
            disposable=True,
        ),
        actor,
    )
    user_task = app.state.task_creation.create(_command(repository.id, actor.user_id), actor)
    removal = TaskRemovalService(
        context.storage, context.workspaces, context.settings.checkpoint_db_path
    )
    assert removal.availability(context.storage.get_task(disposable.task_id), actor).allowed
    assert not removal.availability(context.storage.get_task(user_task.task_id), actor).allowed
    context.sessions.start(
        disposable.task_id,
        HumanIdentity(actor_id=actor.user_id, display_name=actor.display_name),
    )
    removal.remove(disposable.task_id, actor)
    assert context.storage.get_task(disposable.task_id) is None
    assert context.storage.get_task(user_task.task_id) is not None
    assert context.storage.get_events(user_task.task_id)
    assert not context.workspaces.get_path(disposable.task_id).exists()
    with sqlite3.connect(context.settings.checkpoint_db_path) as connection:
        for table in ("checkpoints", "writes"):
            assert (
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE thread_id = ?", (disposable.task_id,)
                ).fetchone()[0]
                == 0
            )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_disposable_cleanup_runs_after_smoke_exception(tmp_path: Path) -> None:
    context, app, repository, actor, _source = _setup(tmp_path)
    disposable = app.state.task_creation.create(
        _command(
            repository.id,
            actor.user_id,
            origin=TaskOrigin.SYSTEM_TEST,
            disposable=True,
        ),
        actor,
    )
    removal = TaskRemovalService(
        context.storage, context.workspaces, context.settings.checkpoint_db_path
    )
    with pytest.raises(RuntimeError, match="halfway"):
        try:
            raise RuntimeError("halfway through smoke")
        finally:
            removal.remove(disposable.task_id, actor)
    assert context.storage.get_task(disposable.task_id) is None
    assert not context.workspaces.get_path(disposable.task_id).exists()


def test_clean_shared_baseline_does_not_leak_obsolete_demo_tests(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "demo_repo"
    provider = LocalWorkspaceProvider(tmp_path / "workspaces", source)
    fresh = provider.create("UNRELATED")
    assert not (source / "tests/test_messages.py").exists()
    assert not (source / "tests/test_users.py").exists()
    assert not (fresh / "tests/test_messages.py").exists()
    assert not (fresh / "tests/test_users.py").exists()
    assert (fresh / "tests/test_discounts.py").is_file()
