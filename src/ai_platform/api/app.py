"""FastAPI application factory for the AI Platform web interface."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from ai_platform.api.presenters import Presenter
from ai_platform.api.routes.auth import router as auth_router
from ai_platform.api.routes.catalog import router as catalog_router
from ai_platform.api.routes.claude_commands import router as claude_commands_router
from ai_platform.api.routes.commands import router as commands_router
from ai_platform.api.routes.config import router as config_router
from ai_platform.api.routes.controls import router as controls_router
from ai_platform.api.routes.health import router as health_router
from ai_platform.api.routes.messages import router as messages_router
from ai_platform.api.routes.models import router as models_router
from ai_platform.api.routes.presence import router as presence_router
from ai_platform.api.routes.stream import router as stream_router
from ai_platform.api.routes.tasks import router as tasks_router
from ai_platform.api.security import SameOriginMutationMiddleware, require_user
from ai_platform.application import ApplicationContext, create_application_context
from ai_platform.auth import AuthService, InvalidCredentialsError
from ai_platform.claude_commands import ClaudeCommandService
from ai_platform.commands import (
    CommandArgumentError,
    CommandParseError,
    CommandService,
    CommandUnavailableError,
    UnknownCommandError,
)
from ai_platform.controls import (
    ActionConflictError,
    PermissionDeniedError,
    RunnerUnavailableError,
    TaskControlService,
)
from ai_platform.conversation import (
    ConversationService,
    IdempotencyConflictError,
    MessageNotAcceptedError,
)
from ai_platform.model_preferences import ModelPreferenceService
from ai_platform.presence import PresenceService
from ai_platform.removal import (
    TaskRemovalConflictError,
    TaskRemovalPermissionError,
    TaskRemovalService,
)
from ai_platform.repositories import RepositoryError
from ai_platform.router import ModelRoutingError
from ai_platform.runner import TaskTurnRunner
from ai_platform.sessions import ExecutorUnavailableError, TaskNotFoundError, TaskSessionError
from ai_platform.task_creation import (
    TaskCreationError,
    TaskCreationService,
    TaskProvisioningError,
)
from ai_platform.workflow_services import WorkflowConflictError, WorkflowError

logger = logging.getLogger(__name__)


def _wire(
    application: FastAPI,
    context: ApplicationContext,
    runner: TaskTurnRunner | None,
) -> None:
    settings = context.settings
    auth = AuthService(context.storage, session_ttl=timedelta(hours=settings.session_hours))
    application.state.context = context
    application.state.runner = runner
    application.state.auth = auth
    application.state.task_creation = TaskCreationService(
        context.storage, context.repositories, auth, context.workspaces
    )
    application.state.removal = TaskRemovalService(
        context.storage, context.workspaces, settings.checkpoint_db_path
    )
    application.state.cookie_secure = settings.cookie_secure
    application.state.presence = PresenceService(context.storage, auth)
    application.state.presenter = Presenter(settings, auth.display_names)
    application.state.conversation = ConversationService(
        context.sessions,
        context.storage,
        on_submitted=runner.wake if runner is not None else None,
    )
    controls = TaskControlService(context.sessions, context.storage, runner)
    application.state.controls = controls
    application.state.commands = CommandService(context.sessions, context.storage, controls)
    application.state.claude_commands = ClaudeCommandService(
        context.sessions, context.storage, settings
    )
    application.state.model_preferences = ModelPreferenceService(
        context.storage, context.model_catalog
    )


def create_app(
    context: ApplicationContext | None = None,
    *,
    runner: TaskTurnRunner | None = None,
    stream_poll_seconds: float = 0.5,
    stream_keepalive_seconds: float = 10.0,
) -> FastAPI:
    """Create the API around the configured core, or around an injected one (tests).

    With the configured core, the message runner starts only when
    ``AI_PLATFORM_ENABLE_RUNNER`` is set. With an injected core, the caller owns
    the (optional) runner's lifecycle.
    """

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        started: TaskTurnRunner | None = None
        if context is None:
            if os.environ.get("AI_PLATFORM_WEB_ACTOR"):
                logger.warning(
                    "AI_PLATFORM_WEB_ACTOR is ignored since Demo 2 Phase 4: web identity "
                    "comes from login sessions (see `ai-platform users --help`)."
                )
            # No stale-lock recovery here: browsing must never change task state.
            configured = create_application_context()
            if configured.settings.enable_runner:
                started = TaskTurnRunner(configured.sessions, configured.storage)
            _wire(application, configured, started)
            if started is not None:
                started.start()
                logger.info("Message runner started")
        try:
            yield
        finally:
            if started is not None:
                started.stop()

    settings_origins = context.settings.allowed_origins if context is not None else ()
    application = FastAPI(title="AI Platform API", version="0.8.0", lifespan=lifespan)
    application.state.stream_poll_seconds = stream_poll_seconds
    application.state.stream_keepalive_seconds = stream_keepalive_seconds
    if context is not None:
        _wire(application, context, runner)
    application.add_middleware(
        SameOriginMutationMiddleware,
        allowed_origins=settings_origins or _env_allowed_origins(),
    )

    def error(status_code: int, message: str) -> JSONResponse:
        # Client-safe messages still pass the path redactor before leaving the server.
        presenter = getattr(application.state, "presenter", None)
        detail = presenter.redactor.text(message) if presenter is not None else message
        return JSONResponse(status_code=status_code, content={"detail": detail})

    @application.exception_handler(InvalidCredentialsError)
    async def invalid_credentials(_request: Request, _exc: InvalidCredentialsError) -> JSONResponse:
        return error(401, "Invalid credentials")

    @application.exception_handler(PermissionDeniedError)
    @application.exception_handler(TaskRemovalPermissionError)
    async def permission_denied(
        _request: Request, exc: PermissionDeniedError | TaskRemovalPermissionError
    ) -> JSONResponse:
        return error(403, str(exc))

    @application.exception_handler(TaskNotFoundError)
    async def task_not_found(_request: Request, exc: TaskNotFoundError) -> JSONResponse:
        return error(404, str(exc))

    @application.exception_handler(MessageNotAcceptedError)
    async def not_accepted(_request: Request, exc: MessageNotAcceptedError) -> JSONResponse:
        return error(409, f"This task cannot receive messages in its current state: {exc}")

    @application.exception_handler(IdempotencyConflictError)
    async def idempotency_conflict(
        _request: Request, exc: IdempotencyConflictError
    ) -> JSONResponse:
        return error(409, str(exc))

    @application.exception_handler(ActionConflictError)
    @application.exception_handler(TaskRemovalConflictError)
    async def action_conflict(
        _request: Request, exc: ActionConflictError | TaskRemovalConflictError
    ) -> JSONResponse:
        return error(409, str(exc))

    @application.exception_handler(CommandUnavailableError)
    async def command_unavailable(_request: Request, exc: CommandUnavailableError) -> JSONResponse:
        return error(409, str(exc))

    @application.exception_handler(CommandArgumentError)
    async def command_argument_error(_request: Request, exc: CommandArgumentError) -> JSONResponse:
        return error(422, str(exc))

    @application.exception_handler(CommandParseError)
    @application.exception_handler(UnknownCommandError)
    async def command_validation_error(
        _request: Request, exc: CommandParseError | UnknownCommandError
    ) -> JSONResponse:
        return error(400, str(exc))

    @application.exception_handler(WorkflowConflictError)
    async def workflow_conflict(_request: Request, exc: WorkflowConflictError) -> JSONResponse:
        return error(409, str(exc))

    @application.exception_handler(WorkflowError)
    async def workflow_error(_request: Request, exc: WorkflowError) -> JSONResponse:
        return error(400, str(exc))

    @application.exception_handler(ModelRoutingError)
    async def model_routing_error(_request: Request, exc: ModelRoutingError) -> JSONResponse:
        return error(400, str(exc))

    @application.exception_handler(RunnerUnavailableError)
    async def runner_unavailable(_request: Request, exc: RunnerUnavailableError) -> JSONResponse:
        return error(503, str(exc))

    @application.exception_handler(ExecutorUnavailableError)
    async def executor_unavailable(
        _request: Request, exc: ExecutorUnavailableError
    ) -> JSONResponse:
        return error(503, f"The agent executor is unavailable: {exc}")

    @application.exception_handler(TaskSessionError)
    async def task_session_error(_request: Request, exc: TaskSessionError) -> JSONResponse:
        return error(400, str(exc))

    @application.exception_handler(RepositoryError)
    @application.exception_handler(TaskCreationError)
    async def creation_validation_error(
        _request: Request, exc: RepositoryError | TaskCreationError
    ) -> JSONResponse:
        return error(400, str(exc))

    @application.exception_handler(TaskProvisioningError)
    async def provisioning_error(_request: Request, exc: TaskProvisioningError) -> JSONResponse:
        return error(503, str(exc))

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, _exc: Exception) -> JSONResponse:
        # Details (paths, Git output, tracebacks) stay in the server log only.
        logger.exception("Unhandled API error for %s %s", request.method, request.url.path)
        return error(500, "Internal server error")

    # Unauthenticated: health and the auth endpoints (login decides for itself).
    application.include_router(health_router, prefix="/api")
    application.include_router(auth_router, prefix="/api")
    # Everything else requires a live session (401); mutations add role checks (403).
    for router in (
        config_router,
        catalog_router,
        models_router,
        tasks_router,
        commands_router,
        claude_commands_router,
        messages_router,
        controls_router,
        presence_router,
        stream_router,
    ):
        application.include_router(router, prefix="/api", dependencies=[Depends(require_user)])
    return application


def _env_allowed_origins() -> tuple[str, ...]:
    raw = os.environ.get("AI_PLATFORM_ALLOWED_ORIGINS", "")
    return tuple(item.strip().rstrip("/") for item in raw.split(",") if item.strip())


app = create_app()
