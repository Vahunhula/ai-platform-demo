"""Task conversation endpoints: read the durable conversation, submit one message."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ai_platform.api.dependencies import ConversationDependency, PresenterDependency
from ai_platform.api.schemas import MessageResponse, PostMessageRequest, PostMessageResponse
from ai_platform.api.security import DeveloperDependency

router = APIRouter(prefix="/tasks", tags=["messages"])


@router.get("/{task_id}/messages", response_model=list[MessageResponse])
def list_messages(
    task_id: str,
    conversation: ConversationDependency,
    presenter: PresenterDependency,
) -> list[MessageResponse]:
    """Return the full Chat projection: conversation, phase output, and gate outcomes.

    Named ``/messages`` for URL compatibility; the response is the browser's
    Chat timeline, not only HUMAN_MESSAGE/AGENT_MESSAGE events.
    """

    return [presenter.chat_item(item) for item in conversation.list_chat_items(task_id)]


@router.post(
    "/{task_id}/messages",
    status_code=202,
    response_model=PostMessageResponse,
    responses={401: {}, 403: {}, 404: {}, 409: {}, 422: {}},
)
def post_message(
    task_id: str,
    body: PostMessageRequest,
    conversation: ConversationDependency,
    user: DeveloperDependency,
) -> JSONResponse:
    """Persist the message and queue its agent turn; never waits for the agent.

    The author is the authenticated session user; the body cannot name one.
    """

    result = conversation.submit(task_id, body.message, body.client_message_id, user.human)
    response = PostMessageResponse(
        message_id=result.message.message_id,
        client_message_id=result.message.client_message_id,
        task_id=result.message.task_id,
        message_status=result.message.status.value.upper(),
        duplicate=not result.created,
    )
    return JSONResponse(status_code=202, content=response.model_dump(mode="json"))
