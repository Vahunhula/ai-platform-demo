"""Task presence: an explicit heartbeat (POST) and a read (GET).

The heartbeat is a POST so that ordinary GETs stay observational. Presence is
ephemeral (TTL) and never enters the task's event history.
"""

from fastapi import APIRouter, Request

from ai_platform.api.dependencies import ContextDependency
from ai_platform.api.schemas import PresenceResponse, PresenceUser
from ai_platform.api.security import UserDependency
from ai_platform.auth import AuthenticatedUser
from ai_platform.presence import PresenceService

router = APIRouter(prefix="/tasks", tags=["presence"])


def _response(task_id: str, viewers: list[AuthenticatedUser]) -> PresenceResponse:
    return PresenceResponse(
        task_id=task_id,
        viewers=[
            PresenceUser(
                user_id=viewer.user_id,
                username=viewer.username,
                display_name=viewer.display_name,
            )
            for viewer in viewers
        ],
    )


@router.post("/{task_id}/presence", response_model=PresenceResponse)
def heartbeat(
    task_id: str, request: Request, user: UserDependency, context: ContextDependency
) -> PresenceResponse:
    """Mark the authenticated user as viewing the task; returns who is viewing it."""

    task = context.sessions.get_definition(task_id)
    presence: PresenceService = request.app.state.presence
    return _response(task.id, presence.heartbeat(task.id, user))


@router.get("/{task_id}/presence", response_model=PresenceResponse)
def viewers(
    task_id: str, request: Request, _user: UserDependency, context: ContextDependency
) -> PresenceResponse:
    task = context.sessions.get_definition(task_id)
    presence: PresenceService = request.app.state.presence
    return _response(task.id, presence.active(task.id))
