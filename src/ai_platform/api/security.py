"""HTTP authentication, authorization and cross-site protection.

Three separate concerns, three separate outcomes:

- authentication (who are you?)          → 401  ``require_user``
- authorization  (may your role do it?)  → 403  ``require_developer``
- task state     (is it valid now?)      → 409  (services/core)

The identity always comes from the server-side session behind the HttpOnly
cookie; request bodies, headers and query strings never carry an actor.
"""

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from ai_platform.auth import AuthenticatedUser, AuthService

SESSION_COOKIE = "ai_platform_session"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def session_secret(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def require_user(request: Request) -> AuthenticatedUser:
    """Resolve the session cookie to a live user or fail with 401."""

    auth: AuthService = request.app.state.auth
    user = auth.resolve(session_secret(request))
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    request.state.user = user
    return user


UserDependency = Annotated[AuthenticatedUser, Depends(require_user)]


def require_developer(user: UserDependency) -> AuthenticatedUser:
    """Task mutations (messages and lifecycle controls) need developer or admin."""

    if not user.role.can_modify_tasks:
        raise HTTPException(
            status_code=403, detail="Read-only access: your role (viewer) cannot change tasks."
        )
    return user


DeveloperDependency = Annotated[AuthenticatedUser, Depends(require_developer)]


def set_session_cookie(
    response: Response, secret: str, *, max_age: int, secure: bool
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        secret,
        max_age=max_age,
        path="/",
        httponly=True,
        samesite="strict",
        secure=secure,
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(
        SESSION_COOKIE, path="/", httponly=True, samesite="strict", secure=secure
    )


class SameOriginMutationMiddleware(BaseHTTPMiddleware):
    """Reject cross-site browser requests that could change state.

    Complements the SameSite=Strict session cookie: for any non-GET request under
    /api, a browser-supplied ``Sec-Fetch-Site: cross-site`` or an ``Origin`` whose
    host differs from the request's own ``Host`` (and is not explicitly allowed)
    is refused with 403. Requests without these headers (curl, scripts) carry no
    ambient browser cookie and are unaffected.
    """

    def __init__(self, app, allowed_origins: tuple[str, ...] = ()) -> None:  # noqa: ANN001
        super().__init__(app)
        self.allowed_origins = {origin.lower() for origin in allowed_origins}

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in _SAFE_METHODS and request.url.path.startswith("/api/"):
            if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
                return _forbidden()
            origin = request.headers.get("origin")
            if origin is not None and not self._same_origin(origin, request):
                return _forbidden()
        return await call_next(request)

    def _same_origin(self, origin: str, request: Request) -> bool:
        normalized = origin.strip().rstrip("/").lower()
        if normalized in self.allowed_origins:
            return True
        host = request.headers.get("host", "").lower()
        return bool(host) and urlsplit(normalized).netloc == host


def _forbidden() -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": "Cross-site request refused"})
