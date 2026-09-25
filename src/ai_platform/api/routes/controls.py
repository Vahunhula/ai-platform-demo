"""Task lifecycle control endpoints: thin wrappers around TaskControlService.

The actor is always the authenticated session user (developer or admin role);
no request body accepts an actor, command, path or option beyond the fields the
CLI command itself takes.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ai_platform.api.dependencies import ContextDependency, ControlsDependency
from ai_platform.api.schemas import (
    ControlResponse,
    RejectRequest,
    ResetRequest,
    ResumeRequest,
    StartRequest,
)
from ai_platform.api.security import DeveloperDependency
from ai_platform.application import ApplicationContext
from ai_platform.controls import ControlResult

router = APIRouter(prefix="/tasks", tags=["controls"])
_RESPONSES = {401: {}, 403: {}, 404: {}, 409: {}, 422: {}, 503: {}}


def _respond(context: ApplicationContext, result: ControlResult) -> JSONResponse:
    record = context.sessions.get_session(result.task_id).record
    body = ControlResponse(
        status="accepted" if result.turn_started else "completed",
        action=result.action.value,
        task_id=result.task_id,
        task_status=record.status.value.upper(),
        execution_id=result.execution_id,
        client_action_id=result.client_action_id,
        duplicate=result.duplicate,
        deferred=result.deferred,
    )
    return JSONResponse(
        status_code=202 if result.turn_started else 200,
        content=body.model_dump(mode="json"),
    )


@router.post("/{task_id}/start", status_code=202, response_model=ControlResponse,
             responses=_RESPONSES)
def start_task(
    task_id: str,
    body: StartRequest,
    controls: ControlsDependency,
    context: ContextDependency,
    user: DeveloperDependency,
) -> JSONResponse:
    """Start a READY task; the initial agent turn runs in the background."""

    return _respond(context, controls.start(task_id, body.client_action_id, user))


@router.post("/{task_id}/resume", status_code=202, response_model=ControlResponse,
             responses=_RESPONSES)
def resume_task(
    task_id: str,
    body: ResumeRequest,
    controls: ControlsDependency,
    context: ContextDependency,
    user: DeveloperDependency,
) -> JSONResponse:
    """Resume a paused task (optional instruction); the turn runs in the background."""

    return _respond(
        context, controls.resume(task_id, body.client_action_id, body.message, user)
    )


@router.post("/{task_id}/reject", status_code=202, response_model=ControlResponse,
             responses=_RESPONSES)
def reject_task(
    task_id: str,
    body: RejectRequest,
    controls: ControlsDependency,
    context: ContextDependency,
    user: DeveloperDependency,
) -> JSONResponse:
    """Reject with feedback; a correction turn runs in the background."""

    return _respond(
        context, controls.reject(task_id, body.client_action_id, body.message, user)
    )


@router.post("/{task_id}/pause", response_model=ControlResponse, responses=_RESPONSES)
def pause_task(
    task_id: str,
    controls: ControlsDependency,
    context: ContextDependency,
    user: DeveloperDependency,
) -> JSONResponse:
    """Pause now, or request a cooperative pause of the active agent turn."""

    return _respond(context, controls.pause(task_id, user))


@router.post("/{task_id}/approve", response_model=ControlResponse, responses=_RESPONSES)
def approve_task(
    task_id: str,
    controls: ControlsDependency,
    context: ContextDependency,
    user: DeveloperDependency,
) -> JSONResponse:
    """Approve a verified task: it becomes COMPLETED (no commit, push or merge)."""

    return _respond(context, controls.approve(task_id, user))


@router.post("/{task_id}/defer", response_model=ControlResponse, responses=_RESPONSES)
def defer_task(
    task_id: str,
    controls: ControlsDependency,
    context: ContextDependency,
    user: DeveloperDependency,
) -> JSONResponse:
    """Record an explicit terminal DEFERRED disposition."""

    return _respond(context, controls.defer(task_id, user))


@router.post("/{task_id}/reset", response_model=ControlResponse, responses=_RESPONSES)
def reset_task(
    task_id: str,
    body: ResetRequest,
    controls: ControlsDependency,
    context: ContextDependency,
    user: DeveloperDependency,
) -> JSONResponse:
    """Delete the workspace and return to READY; history is kept."""

    del body  # its only field is the mandatory confirmation
    return _respond(context, controls.reset(task_id, user))
