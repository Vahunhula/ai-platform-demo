"""FastAPI dependencies for the shared application context."""

from fastapi import Request

from ai_platform.application import ApplicationContext


async def get_context(request: Request) -> ApplicationContext:
    """Return the process-wide core composition created at API startup."""

    return request.app.state.context
