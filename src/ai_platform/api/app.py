"""FastAPI application factory for the read-only web product shell."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ai_platform.api.routes.health import router as health_router
from ai_platform.api.routes.tasks import router as tasks_router
from ai_platform.application import ApplicationContext, create_application_context
from ai_platform.sessions import TaskNotFoundError, TaskSessionError

logger = logging.getLogger(__name__)


def create_app(context: ApplicationContext | None = None) -> FastAPI:
    """Create an API using either the configured or an injected platform core."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if context is None:
            # No stale-lock recovery: browsing must never change task state.
            application.state.context = create_application_context()
        yield

    application = FastAPI(
        title="AI Platform API",
        version="0.6.0",
        lifespan=lifespan,
    )
    if context is not None:
        application.state.context = context

    @application.exception_handler(TaskNotFoundError)
    async def task_not_found(_request: Request, error: TaskNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @application.exception_handler(TaskSessionError)
    async def task_session_error(_request: Request, error: TaskSessionError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(error)})

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, _error: Exception) -> JSONResponse:
        # Details (paths, Git output, tracebacks) stay in the server log only.
        logger.exception("Unhandled API error for %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    application.include_router(health_router, prefix="/api")
    application.include_router(tasks_router, prefix="/api")
    return application


app = create_app()
