"""Unauthenticated provider webhooks authenticated by provider signatures."""

from fastapi import APIRouter, Header, Request

from ai_platform.api.dependencies import ContextDependency

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/github")
async def github_webhook(
    request: Request,
    context: ContextDependency,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_delivery: str = Header(default=""),
    x_github_event: str = Header(default=""),
):
    accepted = context.integrations.github_webhook(
        await request.body(), x_hub_signature_256, x_github_delivery, x_github_event
    )
    return {"accepted": accepted}
