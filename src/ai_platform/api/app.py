"""FastAPI application factory for the AI Platform web interface."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ai_platform.api.presenters import Presenter
from ai_platform.api.routes.config import router as config_router
from ai_platform.api.routes.controls import router as controls_router
from ai_platform.api.routes.health import router as health_router
from ai_platform.api.routes.messages import router as messages_router
from ai_platform.api.routes.stream import router as stream_router
from ai_platform.api.routes.tasks import router as tasks_router
from ai_platform.application import ApplicationContext, create_application_context
from ai_platform.controls import ActionConflictError, RunnerUnavailableError, TaskControlService
from ai_platform.conversation import (
    ConversationService,
    IdempotencyConflictError,
    MessageNotAcceptedError,
    MessagingDisabledError,
)
from ai_platform.runner import TaskTurnRunner
from ai_platform.sessions import ExecutorUnavailableError, TaskNotFoundError, TaskSessionError

logger = logging.getLogger(__name__)


def _wire(
    application: FastAPI,
    context: ApplicationContext,
    runner: TaskTurnRunner | None,
) -> None:
    application.state.context = context
    application.state.runner = runner
    application.state.presenter = Presenter(context.settings)
    application.state.conversation = ConversationService(
        context.sessions,
        context.storage,
        context.settings.web_actor,
        on_submitted=runner.wake if runner is not None else None,
    )
    application.state.controls = TaskControlService(
        context.sessions, context.storage, application.state.conversation, runner
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

    application = FastAPI(title="AI Platform API", version="0.7.0", lifespan=lifespan)
    application.state.stream_poll_seconds = stream_poll_seconds
    application.state.stream_keepalive_seconds = stream_keepalive_seconds
    if context is not None:
        _wire(application, context, runner)

    def error(status_code: int, message: str) -> JSONResponse:
        # Client-safe messages still pass the path redactor before leaving the server.
        presenter = getattr(application.state, "presenter", None)
        detail = presenter.redactor.text(message) if presenter is not None else message
        return JSONResponse(status_code=status_code, content={"detail": detail})

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
    async def action_conflict(_request: Request, exc: ActionConflictError) -> JSONResponse:
        return error(409, str(exc))

    @application.exception_handler(RunnerUnavailableError)
    async def runner_unavailable(_request: Request, exc: RunnerUnavailableError) -> JSONResponse:
        return error(503, str(exc))

    @application.exception_handler(ExecutorUnavailableError)
    async def executor_unavailable(
        _request: Request, exc: ExecutorUnavailableError
    ) -> JSONResponse:
        return error(503, f"The agent executor is unavailable: {exc}")

    @application.exception_handler(MessagingDisabledError)
    async def messaging_disabled(_request: Request, exc: MessagingDisabledError) -> JSONResponse:
        return error(503, str(exc))

    @application.exception_handler(TaskSessionError)
    async def task_session_error(_request: Request, exc: TaskSessionError) -> JSONResponse:
        return error(400, str(exc))

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, _exc: Exception) -> JSONResponse:
        # Details (paths, Git output, tracebacks) stay in the server log only.
        logger.exception("Unhandled API error for %s %s", request.method, request.url.path)
        return error(500, "Internal server error")

    for router in (
        health_router,
        config_router,
        tasks_router,
        messages_router,
        controls_router,
        stream_router,
    ):
        application.include_router(router, prefix="/api")
    return application


app = create_app()
