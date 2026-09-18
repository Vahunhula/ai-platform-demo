"""Login, current user and logout for the local access-token provider."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ai_platform.api.schemas import LoginRequest, UserResponse
from ai_platform.api.security import (
    UserDependency,
    clear_session_cookie,
    session_secret,
    set_session_cookie,
)
from ai_platform.auth import AuthenticatedUser, AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


def user_response(user: AuthenticatedUser) -> UserResponse:
    return UserResponse(
        id=user.user_id,
        username=user.username,
        display_name=user.display_name,
        role=user.role.value,
        can_modify_tasks=user.role.can_modify_tasks,
    )


@router.post("/login", response_model=UserResponse, responses={401: {}})
def login(body: LoginRequest, request: Request) -> JSONResponse:
    """Exchange username + access token for an HttpOnly session cookie."""

    auth: AuthService = request.app.state.auth
    session = auth.login(body.username, body.token)  # InvalidCredentialsError → 401
    response = JSONResponse(content=user_response(session.user).model_dump(mode="json"))
    set_session_cookie(
        response,
        session.secret,
        max_age=int(auth.session_ttl.total_seconds()),
        secure=request.app.state.cookie_secure,
    )
    return response


@router.get("/me", response_model=UserResponse)
def me(user: UserDependency) -> UserResponse:
    return user_response(user)


@router.post("/logout", status_code=204)
def logout(request: Request) -> Response:
    """Revoke the current session (if any) and clear the cookie."""

    auth: AuthService = request.app.state.auth
    auth.logout(session_secret(request))
    response = Response(status_code=204)
    clear_session_cookie(response, secure=request.app.state.cookie_secure)
    return response
