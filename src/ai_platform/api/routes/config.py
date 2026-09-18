"""What this API process lets the browser do."""

from fastapi import APIRouter

from ai_platform.api.dependencies import ContextDependency, ConversationDependency, RunnerDependency
from ai_platform.api.schemas import ConfigResponse
from ai_platform.sessions import MAX_MESSAGE_LENGTH

router = APIRouter(tags=["config"])


@router.get("/config", response_model=ConfigResponse)
def get_config(
    context: ContextDependency,
    conversation: ConversationDependency,
    runner: RunnerDependency,
) -> ConfigResponse:
    return ConfigResponse(
        messaging_enabled=conversation.messaging_enabled,
        web_actor=context.settings.web_actor,
        runner_enabled=runner is not None,
        max_message_length=MAX_MESSAGE_LENGTH,
    )
