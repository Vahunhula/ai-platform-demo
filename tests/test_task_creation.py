"""Demo 2.5 task creation and workspace-isolation regression tests."""

import asyncio
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from ai_platform.api.app import create_app
from ai_platform.auth import AuthenticatedUser, Role
from ai_platform.cli import app as cli_app
from ai_platform.events import EventType
from ai_platform.executors.base import ExecutionRequest
from ai_platform.identity import HumanIdentity
from ai_platform.models import VerificationStatus
from ai_platform.repositories import RepositoryError
from ai_platform.sessions import TaskSessionError
from ai_platform.task_creation import (
    CreateTaskCommand,
    TaskCreationService,
    TaskProvisioningError,
)
from ai_platform.workspace import WorkspaceError
from tests.fakes import FakeAgentExecutor
from tests.test_messaging import _client, _context


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _branch(root: Path) -> str:
    return subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _setup(tmp_path: Path):
    context = _context(tmp_path)
    app = create_app(context)
    auth = app.state.auth
    alex = auth.add_user("alex", "Alex", Role.DEVELOPER)
    root = Path(__file__).parents[1]
    repository = context.repositories.register_local(
        "python-demo", "Python Demo Repository", root / "demo_repo", _branch(root)
    )
    return context, app, repository, alex


def _command(repository_id: str, assignee_id: str, **overrides: str | None):
    values = {
        "title": "Validate discount input",
        "description": "Ensure negative quantities are handled safely.",
        "repository_id": repository_id,
        "base_branch": _branch(Path(__file__).parents[1]),
        "assignee_user_id": assignee_id,
        "jira_key": "APP-25",
    }
    values.update(overrides)
    return CreateTaskCommand(**values)


def _actor() -> AuthenticatedUser:
    return AuthenticatedUser("creator-id", "vakho", "Vakho", Role.DEVELOPER)


def _workspace_entries(context) -> set[Path]:
    root = context.settings.workspace_root
    return set(root.iterdir()) if root.exists() else set()


class IsolatedWritingExecutor(FakeAgentExecutor):
    """Exercise the real execution path while making the demo suite pass."""

    def __init__(self) -> None:
        super().__init__(fix_on_attempt=None)

    def execute(self, request: ExecutionRequest):
        workspace = request.workspace_path
        marker = "agent-a.txt" if request.task.title == "Isolation A" else "agent-b.txt"
        (workspace / marker).write_text(request.task.id, encoding="utf-8")
        messages = workspace / "app" / "messages.py"
        messages.write_text(
            messages.read_text(encoding="utf-8").replace("Welocme", "Welcome"),
            encoding="utf-8",
        )
        discounts = workspace / "app" / "discounts.py"
        discounts.write_text(
            discounts.read_text(encoding="utf-8").replace("return 0.05", "return 0.10"),
            encoding="utf-8",
        )
        users = workspace / "app" / "users.py"
        users.write_text(
            users.read_text(encoding="utf-8").replace(
                'return f"{last_name.strip()}, {first_name.strip()}"',
                "return profile_display_name(first_name, last_name)",
            ),
            encoding="utf-8",
        )
        return super().execute(request)


def test_repository_registry_validates_lists_and_disables(tmp_path: Path) -> None:
    context, _app, repository, _alex = _setup(tmp_path)

    listed = context.repositories.list()
    assert listed == [repository]
    assert repository.source_type == "local_git"
    with pytest.raises(RepositoryError, match="already exists"):
        context.repositories.register_local(
            repository.slug,
            repository.display_name,
            Path(repository.source),
            repository.default_branch,
        )
    with pytest.raises(RepositoryError, match="does not exist"):
        context.repositories.register_local("missing", "Missing", tmp_path / "nope", "main")
    with pytest.raises(RepositoryError, match="validated"):
        context.repositories.validate_branch(Path(repository.source), "missing-branch")

    disabled = context.repositories.disable(repository.slug)
    assert not disabled.enabled
    assert context.repositories.list(enabled_only=True) == []


def test_repository_cli_add_list_and_disable(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    env = {
        "AI_PLATFORM_DATA_DIR": str(tmp_path / "data"),
        "AI_PLATFORM_WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        "AI_PLATFORM_DB_PATH": str(tmp_path / "data" / "platform.db"),
        "AI_PLATFORM_CHECKPOINT_DB_PATH": str(tmp_path / "data" / "checkpoints.db"),
        "COLUMNS": "200",
    }
    runner = CliRunner()
    added = runner.invoke(
        cli_app,
        [
            "repository",
            "add",
            "--slug",
            "python-demo",
            "--display-name",
            "Python Demo Repository",
            "--source",
            str(root / "demo_repo"),
            "--default-branch",
            _branch(root),
        ],
        env=env,
    )
    listed = runner.invoke(cli_app, ["repository", "list"], env=env)
    disabled = runner.invoke(
        cli_app, ["repository", "disable", "python-demo"], env=env
    )
    assert added.exit_code == listed.exit_code == disabled.exit_code == 0
    assert "python-demo" in listed.stdout and "Python Demo Repository" in listed.stdout
    assert str(root) not in listed.stdout
    assert "disabled" in disabled.stdout


@pytest.mark.anyio
async def test_safe_catalogs_and_developer_create_task(tmp_path: Path) -> None:
    context, app, repository, alex = _setup(tmp_path)
    async with _client(app, "vakho") as client:
        repositories = await client.get("/api/repositories")
        users = await client.get("/api/users/assignable")
        response = await client.post(
            "/api/tasks", json=asdict(_command(repository.id, alex.user_id))
        )

    assert repositories.json() == [
        {
            "id": repository.id,
            "slug": "python-demo",
            "display_name": "Python Demo Repository",
            "default_branch": repository.default_branch,
            "enabled": True,
        }
    ]
    assert "source" not in repositories.text
    assert users.json() == [
        {"id": alex.user_id, "username": "alex", "display_name": "Alex"},
        {
            "id": users.json()[1]["id"],
            "username": "vakho",
            "display_name": "Vakho",
        },
    ]
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["id"].startswith("TASK-") and created["status"] == "READY"
    assert created["workflow_phase"] == "BRAINSTORM"
    assert created["created_by"] == "vakho" and created["workspace_ready"] is True
    assert "workspace" not in created or "workspace_path" not in created
    record = context.storage.get_task(created["id"])
    assert record is not None and record.repository_id == repository.id
    workspace = context.workspaces.get_path(created["id"])
    assert workspace.is_dir() and (workspace / "app" / "discounts.py").is_file()
    events = context.storage.get_events(created["id"])
    assert {event.event_type for event in events} >= {
        EventType.TASK_CREATED,
        EventType.REPOSITORY_SELECTED,
        EventType.ASSIGNEE_SET,
        EventType.WORKSPACE_PROVISION_STARTED,
        EventType.WORKSPACE_PROVISIONED,
    }
    assert not any(str(workspace) in event.model_dump_json() for event in events)


@pytest.mark.anyio
async def test_create_auth_validation_and_actor_cannot_be_spoofed(tmp_path: Path) -> None:
    context, app, repository, alex = _setup(tmp_path)
    body = asdict(_command(repository.id, alex.user_id))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as anonymous:
        assert (await anonymous.post("/api/tasks", json=body)).status_code == 401
    async with _client(app, "watcher", Role.VIEWER) as viewer:
        assert (await viewer.post("/api/tasks", json=body)).status_code == 403
    async with _client(app, "vakho") as developer:
        spoofed = await developer.post(
            "/api/tasks", json={**body, "created_by": "alex"}
        )
        blank = await developer.post("/api/tasks", json={**body, "title": "  "})
        bad_repository = await developer.post(
            "/api/tasks", json={**body, "repository_id": "repo_missing"}
        )
        bad_assignee = await developer.post(
            "/api/tasks", json={**body, "assignee_user_id": "user_missing"}
        )
        bad_branch = await developer.post(
            "/api/tasks", json={**body, "base_branch": "--upload-pack=evil"}
        )
    assert spoofed.status_code == 422
    assert blank.status_code == 422
    assert (bad_repository.status_code, bad_assignee.status_code, bad_branch.status_code) == (
        400,
        400,
        400,
    )
    assert len(context.sessions.list_tasks()) == 3


@pytest.mark.anyio
async def test_disabled_repository_cannot_create(tmp_path: Path) -> None:
    context, app, repository, alex = _setup(tmp_path)
    context.repositories.disable(repository.slug)
    async with _client(app, "vakho") as client:
        response = await client.post(
            "/api/tasks", json=asdict(_command(repository.id, alex.user_id))
        )
    assert response.status_code == 400
    assert response.json() == {"detail": "Repository is disabled"}


def test_parallel_creation_has_unique_ids_and_workspaces(tmp_path: Path) -> None:
    context, app, repository, alex = _setup(tmp_path)
    creation: TaskCreationService = app.state.task_creation

    def create(index: int):
        return creation.create(
            _command(repository.id, alex.user_id, title=f"Parallel task {index}"), _actor()
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        records = list(pool.map(create, range(8)))
    ids = {record.task_id for record in records}
    paths = {record.workspace_path for record in records}
    assert len(ids) == len(paths) == 8
    assert all(Path(path or "").is_dir() for path in paths)
    first, second = records[:2]
    Path(first.workspace_path or "").joinpath("parallel-a.txt").write_text(
        "parallel A\n", encoding="utf-8"
    )
    Path(second.workspace_path or "").joinpath("parallel-b.txt").write_text(
        "parallel B\n", encoding="utf-8"
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        diffs = list(pool.map(context.sessions.get_diff, [record.task_id for record in records]))
    assert "parallel-a.txt" in diffs[0] and "parallel-b.txt" not in diffs[0]
    assert "parallel-b.txt" in diffs[1] and "parallel-a.txt" not in diffs[1]
    assert all(not diff for diff in diffs[2:])


def test_provision_and_persistence_failures_never_create_successful_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, app, repository, alex = _setup(tmp_path)
    creation: TaskCreationService = app.state.task_creation
    before = _workspace_entries(context)

    monkeypatch.setattr(
        context.workspaces,
        "create",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(WorkspaceError("broken copy")),
    )
    with pytest.raises(TaskProvisioningError, match="provisioning failed"):
        creation.create(_command(repository.id, alex.user_id), _actor())
    assert len(context.sessions.list_tasks()) == 3

    monkeypatch.undo()
    monkeypatch.setattr(
        context.storage,
        "create_managed_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database full")),
    )
    with pytest.raises(TaskProvisioningError, match="persistence failed"):
        creation.create(_command(repository.id, alex.user_id), _actor())
    after = _workspace_entries(context)
    assert after == before
    assert len(context.sessions.list_tasks()) == 3


def test_start_reuses_eager_workspace_and_reset_reprovisions(tmp_path: Path) -> None:
    context, app, repository, alex = _setup(tmp_path)
    record = app.state.task_creation.create(
        _command(repository.id, alex.user_id), _actor()
    )
    workspace = context.workspaces.get_path(record.task_id)
    marker = workspace / "human-note.txt"
    marker.write_text("keep before start", encoding="utf-8")
    (workspace / "app" / "messages.py").write_text(
        'def welcome_message() -> str:\n    return "Welcome to AI Platform"\n', encoding="utf-8"
    )
    discounts = workspace / "app" / "discounts.py"
    discounts.write_text(
        discounts.read_text(encoding="utf-8").replace("return 0.05", "return 0.10"),
        encoding="utf-8",
    )
    users = workspace / "app" / "users.py"
    users.write_text(
        users.read_text(encoding="utf-8").replace(
            'return f"{last_name.strip()}, {first_name.strip()}"',
            "return profile_display_name(first_name, last_name)",
        ),
        encoding="utf-8",
    )

    human = HumanIdentity(actor_id="vakho", display_name="Vakho")
    context.sessions.start(record.task_id, human)
    assert marker.read_text(encoding="utf-8") == "keep before start"
    assert context.storage.get_task(record.task_id).workspace_path == str(workspace)

    context.sessions.reset(record.task_id, human)
    reset_record = context.storage.get_task(record.task_id)
    assert reset_record is not None and reset_record.status.value == "ready"
    assert reset_record.workspace_path == str(workspace) and workspace.is_dir()
    assert not marker.exists()


def test_workspace_runs_python_and_two_tasks_are_isolated(tmp_path: Path) -> None:
    context, app, repository, alex = _setup(tmp_path)
    first = app.state.task_creation.create(_command(repository.id, alex.user_id), _actor())
    second = app.state.task_creation.create(_command(repository.id, alex.user_id), _actor())
    first_path = context.workspaces.get_path(first.task_id)
    second_path = context.workspaces.get_path(second.task_id)
    assert first_path != second_path
    marker = first_path / "isolated.txt"
    marker.write_text("first", encoding="utf-8")
    assert not (second_path / marker.name).exists()
    result = subprocess.run(
        ["python3", "-c", "from app.discounts import discount_rate; print(discount_rate(1))"],
        cwd=second_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "0.0"


@pytest.mark.anyio
async def test_dynamic_task_workspace_isolation_across_every_operation(tmp_path: Path) -> None:
    """A/B regression: create, Diff, Start/agent/tests, and Reset stay isolated."""

    executor = IsolatedWritingExecutor()
    context = _context(tmp_path, executor)
    app = create_app(context)
    alex = app.state.auth.add_user("alex", "Alex", Role.DEVELOPER)
    root = Path(__file__).parents[1]
    repository = context.repositories.register_local(
        "python-demo", "Python Demo Repository", root / "demo_repo", _branch(root)
    )

    async with _client(app, "vakho") as client:
        created = []
        for title in ("Isolation A", "Isolation B"):
            response = await client.post(
                "/api/tasks",
                json=asdict(
                    _command(
                        repository.id,
                        alex.user_id,
                        title=title,
                        description=f"Prove {title} uses only its own workspace.",
                    )
                ),
            )
            assert response.status_code == 201, response.text
            created.append(response.json()["id"])
        task_a, task_b = created
        workspace_a = context.sessions.resolve_workspace(task_a, require_exists=True)
        workspace_b = context.sessions.resolve_workspace(task_b, require_exists=True)
        assert workspace_a != workspace_b
        assert context.storage.get_task(task_a).workspace_path == str(workspace_a)
        assert context.storage.get_task(task_b).workspace_path == str(workspace_b)

        (workspace_a / "only-a.txt").write_text("A only\n", encoding="utf-8")
        (workspace_b / "only-b.txt").write_text("B only\n", encoding="utf-8")

        for _ in range(2):
            response_a, response_b = await asyncio.gather(
                client.get(f"/api/tasks/{task_a}/diff"),
                client.get(f"/api/tasks/{task_b}/diff"),
            )
            diff_a = response_a.json()["diff"]
            diff_b = response_b.json()["diff"]
            assert "only-a.txt" in diff_a and "only-b.txt" not in diff_a
            assert "only-b.txt" in diff_b and "only-a.txt" not in diff_b

    human = HumanIdentity(actor_id="vakho", display_name="Vakho")
    context.sessions.start(task_a, human)
    context.sessions.start(task_b, human)
    requests = {request.task.id: request for request in executor.requests}
    assert requests[task_a].workspace_path == workspace_a
    assert requests[task_b].workspace_path == workspace_b
    assert (workspace_a / "only-a.txt").is_file()
    assert (workspace_b / "only-b.txt").is_file()
    assert (workspace_a / "agent-a.txt").is_file()
    assert not (workspace_a / "agent-b.txt").exists()
    assert (workspace_b / "agent-b.txt").is_file()
    assert not (workspace_b / "agent-a.txt").exists()
    assert context.storage.get_task(task_a).verification_status is VerificationStatus.PASSED
    assert context.storage.get_task(task_b).verification_status is VerificationStatus.PASSED

    before_b = context.workspaces.snapshot(task_b)
    context.sessions.reset(task_a, human)
    assert context.sessions.resolve_workspace(task_a, require_exists=True) == workspace_a
    assert not (workspace_a / "only-a.txt").exists()
    assert context.workspaces.snapshot(task_b) == before_b
    assert (workspace_b / "only-b.txt").read_text(encoding="utf-8") == "B only\n"


def test_managed_workspace_identity_mismatch_never_falls_back(tmp_path: Path) -> None:
    context, app, repository, alex = _setup(tmp_path)
    first = app.state.task_creation.create(_command(repository.id, alex.user_id), _actor())
    second = app.state.task_creation.create(_command(repository.id, alex.user_id), _actor())

    with context.storage.transaction() as connection:
        connection.execute(
            "UPDATE tasks SET workspace_path = ? WHERE task_id = ?",
            (second.workspace_path, first.task_id),
        )

    with pytest.raises(TaskSessionError, match="invalid workspace identity"):
        context.sessions.get_diff(first.task_id)


def test_unsafe_task_ids_cannot_escape_workspace(tmp_path: Path) -> None:
    context, _app, _repository, _alex = _setup(tmp_path)
    with pytest.raises(ValueError, match="Unsafe task ID"):
        context.workspaces.create("../escape")


def test_concurrent_initialize_migrates_additively(tmp_path: Path) -> None:
    from ai_platform.storage import SQLiteStorage

    db = tmp_path / "migration.db"
    storages = [SQLiteStorage(db) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda storage: storage.initialize(), storages))
    with storages[0].transaction() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "repository_id",
        "assignee_user_id",
        "created_by",
        "description",
        "workflow_phase",
    } <= columns
    assert {
        "workflow_artifacts",
        "checklist_evaluations",
        "checklist_evaluation_items",
    } <= tables
