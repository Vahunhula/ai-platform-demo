"""Task-aware platform command catalog and generic execution endpoint."""

from dataclasses import asdict

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ai_platform.api.dependencies import CommandsDependency, PresenterDependency
from ai_platform.api.schemas import (
    CommandMetadataResponse,
    CommandResultResponse,
    ExecuteCommandRequest,
)
from ai_platform.api.security import UserDependency

router = APIRouter(prefix="/tasks", tags=["commands"])


@router.get("/{task_id}/commands", response_model=list[CommandMetadataResponse])
def list_commands(
    task_id: str,
    commands: CommandsDependency,
    user: UserDependency,
) -> list[CommandMetadataResponse]:
    return [CommandMetadataResponse(**asdict(item)) for item in commands.metadata(task_id, user)]


@router.post(
    "/{task_id}/commands",
    response_model=CommandResultResponse,
    responses={400: {}, 401: {}, 403: {}, 404: {}, 409: {}, 422: {}, 503: {}},
)
def execute_command(
    task_id: str,
    body: ExecuteCommandRequest,
    commands: CommandsDependency,
    presenter: PresenterDependency,
    user: UserDependency,
) -> JSONResponse:
    result = commands.execute(task_id, body.command_text, user, body.client_command_id)
    response = CommandResultResponse(
        command=result.command,
        status="accepted" if result.accepted else "completed",
        message=presenter.redactor.text(result.message),
        data=presenter.redactor.value(result.data),
    )
    return JSONResponse(
        status_code=202 if result.accepted else 200,
        content=response.model_dump(mode="json"),
    )
