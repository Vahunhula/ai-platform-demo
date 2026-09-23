"""Safe browser catalogs for registered repositories and assignable developers."""

from fastapi import APIRouter, Request

from ai_platform.api.dependencies import ContextDependency
from ai_platform.api.schemas import AssignableUserResponse, RepositoryResponse
from ai_platform.auth import AuthService

router = APIRouter(tags=["task creation"])


@router.get("/repositories", response_model=list[RepositoryResponse])
def list_repositories(context: ContextDependency) -> list[RepositoryResponse]:
    return [
        RepositoryResponse(
            id=repository.id,
            slug=repository.slug,
            display_name=repository.display_name,
            default_branch=repository.default_branch,
            enabled=repository.enabled,
        )
        for repository in context.repositories.list(enabled_only=True)
    ]


@router.get("/users/assignable", response_model=list[AssignableUserResponse])
def list_assignable_users(request: Request) -> list[AssignableUserResponse]:
    auth: AuthService = request.app.state.auth
    return [
        AssignableUserResponse(
            id=user.user_id,
            username=user.username,
            display_name=user.display_name,
        )
        for user in auth.assignable_users()
    ]
