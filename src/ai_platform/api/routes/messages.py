"""Task conversation endpoints: read the durable conversation, submit one message."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ai_platform.api.dependencies import ConversationDependency, PresenterDependency
from ai_platform.api.schemas import MessageResponse, PostMessageRequest, PostMessageResponse

router = APIRouter(prefix="/tasks", tags=["messages"])


@router.get("/{task_id}/messages", response_model=list[MessageResponse])
def list_messages(
    task_id: str,
    conversation: ConversationDependency,
    presenter: PresenterDependency,
) -> list[MessageResponse]:
    return [presenter.message(entry) for entry in conversation.list_messages(task_id)]


@router.post(
    "/{task_id}/messages",
    status_code=202,
    response_model=PostMessageResponse,
    responses={404: {}, 409: {}, 422: {}, 503: {}},
)
def post_message(
    task_id: str,
    body: PostMessageRequest,
    conversation: ConversationDependency,
) -> JSONResponse:
    """Persist the message and queue its agent turn; never waits for the agent."""

    result = conversation.submit(task_id, body.message, body.client_message_id)
    response = PostMessageResponse(
        message_id=result.message.message_id,
        client_message_id=result.message.client_message_id,
        task_id=result.message.task_id,
        message_status=result.message.status.value.upper(),
        duplicate=not result.created,
    )
    return JSONResponse(status_code=202, content=response.model_dump(mode="json"))
