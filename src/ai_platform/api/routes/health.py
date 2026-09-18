"""Process health endpoint."""

from fastapi import APIRouter

from ai_platform.api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()
