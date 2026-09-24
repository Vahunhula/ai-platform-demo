"""Application composition shared by the CLI and HTTP interface."""

from collections.abc import Callable
from dataclasses import dataclass

from ai_platform.config import Settings
from ai_platform.events import ActorType, Event, EventType
from ai_platform.executors import AgentExecutor, ClaudeAgentExecutor
from ai_platform.models import TaskDefinition
from ai_platform.repositories import RepositoryService
from ai_platform.router import ModelCatalog, ModelRouter
from ai_platform.sessions import TaskSessionError, TaskSessionService
from ai_platform.storage import SQLiteStorage
from ai_platform.task_loader import load_tasks
from ai_platform.workspace import LocalWorkspaceProvider


@dataclass(slots=True)
class ApplicationContext:
    """Configured platform objects shared by all presentation layers."""

    settings: Settings
    definitions: list[TaskDefinition]
    storage: SQLiteStorage
    workspaces: LocalWorkspaceProvider
    repositories: RepositoryService
    model_catalog: ModelCatalog
    model_router: ModelRouter
    sessions: TaskSessionService


def create_application_context(
    settings: Settings | None = None,
    *,
    lock_recovery_client: str | None = None,
    executor_factory: Callable[[], AgentExecutor] | None = None,
) -> ApplicationContext:
    """Compose the platform core and ensure configured task definitions exist.

    ``lock_recovery_client`` names the interface performing startup stale-lock
    recovery (the CLI passes ``"cli-startup"``). ``None`` skips recovery, which
    keeps read-only interfaces such as the HTTP API free of state changes.
    ``executor_factory`` defaults to the configured real executor; tests inject fakes.
    """

    configured = settings or Settings.from_env()
    definitions = load_tasks(configured.tasks_path)
    storage = SQLiteStorage(configured.db_path)
    storage.initialize()
    for definition in definitions:
        if storage.create_task(definition):
            storage.append_event(
                Event(
                    task_id=definition.id,
                    event_type=EventType.TASK_CREATED,
                    actor_type=ActorType.SYSTEM,
                    actor_id="task-loader",
                    metadata={"title": definition.title},
                )
            )
    workspaces = LocalWorkspaceProvider(
        configured.workspace_root,
        configured.demo_repository,
    )
    repositories = RepositoryService(storage, configured.workspace_root)
    model_catalog = ModelCatalog.from_settings(configured)
    model_router = ModelRouter.from_settings(configured, storage)
    sessions = TaskSessionService(
        configured,
        definitions,
        storage,
        workspaces,
        model_router,
        executor_factory or (lambda: create_executor(configured)),
        repositories=repositories,
    )
    if lock_recovery_client is not None:
        sessions.recover_stale_locks(
            recovered_by=(
                f"{lock_recovery_client}@{sessions.locks.hostname}:{sessions.locks.process_id}"
            )
        )
    return ApplicationContext(
        configured,
        definitions,
        storage,
        workspaces,
        repositories,
        model_catalog,
        model_router,
        sessions,
    )


def create_executor(settings: Settings) -> AgentExecutor:
    """Create the configured executor only when a write operation needs one."""

    if settings.executor.lower() == "claude":
        return ClaudeAgentExecutor(settings)
    raise TaskSessionError(
        f"Unsupported executor: {settings.executor}. Set AI_PLATFORM_EXECUTOR=claude."
    )
