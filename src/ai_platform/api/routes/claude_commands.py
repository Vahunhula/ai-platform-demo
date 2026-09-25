"""Discovery and execution endpoints for the allowlisted Claude namespace."""

from dataclasses import asdict

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ai_platform.api.dependencies import ClaudeCommandsDependency, PresenterDependency
from ai_platform.api.schemas import (
    ClaudeCommandMetadataResponse,
    CommandResultResponse,
    ExecuteCommandRequest,
)
from ai_platform.api.security import UserDependency

router = APIRouter(prefix="/tasks", tags=["claude-commands"])


@router.get(
    "/{task_id}/claude-commands", response_model=list[ClaudeCommandMetadataResponse]
)
def list_claude_commands(
    task_id: str,
    commands: ClaudeCommandsDependency,
    user: UserDependency,
) -> list[ClaudeCommandMetadataResponse]:
    return [
        ClaudeCommandMetadataResponse(**asdict(item))
        for item in commands.metadata(task_id, user)
    ]


@router.post(
    "/{task_id}/claude-commands",
    response_model=CommandResultResponse,
    responses={400: {}, 401: {}, 403: {}, 404: {}, 409: {}, 422: {}, 503: {}},
)
def execute_claude_command(
    task_id: str,
    body: ExecuteCommandRequest,
    commands: ClaudeCommandsDependency,
    presenter: PresenterDependency,
    user: UserDependency,
) -> JSONResponse:
    result = commands.execute(task_id, body.command_text, user, body.client_command_id)
    response = CommandResultResponse(
        command=result.command,
        status="completed",
        message=presenter.redactor.text(result.message),
        data=presenter.redactor.value(result.data),
    )
    return JSONResponse(status_code=200, content=response.model_dump(mode="json"))
