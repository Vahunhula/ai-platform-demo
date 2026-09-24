"""Safe public model catalog."""

from fastapi import APIRouter

from ai_platform.api.dependencies import ContextDependency
from ai_platform.api.schemas import ModelCatalogResponse

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=list[ModelCatalogResponse])
def list_models(context: ContextDependency) -> list[ModelCatalogResponse]:
    return [
        ModelCatalogResponse(
            logical_id=entry.logical_id,
            display_name=entry.display_name,
            provider=entry.provider,
            enabled=entry.enabled,
        )
        for entry in context.model_catalog.public_entries()
    ]
