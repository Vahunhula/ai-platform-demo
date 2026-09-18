"""Server capabilities relevant to the browser."""

from fastapi import APIRouter

from ai_platform.api.dependencies import RunnerDependency
from ai_platform.api.schemas import ConfigResponse
from ai_platform.presence import HEARTBEAT_INTERVAL_SECONDS
from ai_platform.sessions import MAX_MESSAGE_LENGTH

router = APIRouter(tags=["config"])


@router.get("/config", response_model=ConfigResponse)
def get_config(runner: RunnerDependency) -> ConfigResponse:
    return ConfigResponse(
        runner_enabled=runner is not None,
        max_message_length=MAX_MESSAGE_LENGTH,
        presence_heartbeat_seconds=HEARTBEAT_INTERVAL_SECONDS,
    )
