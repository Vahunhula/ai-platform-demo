"""Admin-only integration lifecycle routes."""

from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from ai_platform.api.dependencies import ContextDependency
from ai_platform.api.security import AdminDependency
from ai_platform.integrations.models import IntegrationProvider

router = APIRouter(prefix="/integrations", tags=["integrations"])


class JiraSiteSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection_id: str = Field(min_length=5, max_length=100)
    cloud_id: str = Field(min_length=1, max_length=200)


@router.get("")
def list_integrations(context: ContextDependency, user: AdminDependency):
    return context.integrations.list_public(user)


@router.get("/github")
def get_github(context: ContextDependency, user: AdminDependency):
    return context.integrations.get_public(IntegrationProvider.GITHUB, user)


@router.post("/github/connect")
def connect_github(context: ContextDependency, user: AdminDependency):
    return context.integrations.start_github(user)


@router.get("/github/callback")
def github_callback(
    context: ContextDependency,
    request: Request,
    installation_id: int,
    state: str,
    format: Literal["redirect", "json"] = Query(default="redirect"),
):
    user_id = context.integrations.oauth_user(IntegrationProvider.GITHUB, state)
    user = request.app.state.auth.users_by_id([user_id]).get(user_id)
    if user is None:
        from ai_platform.integrations.models import OAuthStateError

        raise OAuthStateError("The user that started authorization is no longer available.")
    result = context.integrations.complete_github(installation_id, state, user)
    if format == "json":
        return result
    return RedirectResponse(url="/?settings=integrations&github=connected", status_code=303)


@router.post("/github/check")
def check_github(context: ContextDependency, user: AdminDependency):
    return context.integrations.check_github(user)


@router.delete("/github", status_code=204)
def disconnect_github(context: ContextDependency, user: AdminDependency) -> None:
    context.integrations.disconnect(IntegrationProvider.GITHUB, user)


@router.get("/jira")
def get_jira(context: ContextDependency, user: AdminDependency):
    return context.integrations.get_public(IntegrationProvider.JIRA, user)


@router.post("/jira/connect")
def connect_jira(context: ContextDependency, user: AdminDependency):
    return context.integrations.start_jira(user)


@router.get("/jira/callback")
def jira_callback(
    context: ContextDependency,
    request: Request,
    code: str,
    state: str,
    format: Literal["redirect", "json"] = Query(default="redirect"),
):
    user_id = context.integrations.oauth_user(IntegrationProvider.JIRA, state)
    user = request.app.state.auth.users_by_id([user_id]).get(user_id)
    if user is None:
        from ai_platform.integrations.models import OAuthStateError

        raise OAuthStateError("The user that started authorization is no longer available.")
    result = context.integrations.complete_jira(code, state, user)
    if format == "json":
        return result
    suffix = (
        f"&jira_selection={result['selection_id']}"
        if result.get("selection_required")
        else "&jira=connected"
    )
    return RedirectResponse(url=f"/?settings=integrations{suffix}", status_code=303)


@router.get("/jira/sites")
def jira_sites(
    context: ContextDependency,
    user: AdminDependency,
    selection_id: str = Query(min_length=5, max_length=100),
):
    return context.integrations.pending_jira_sites(selection_id, user)


@router.post("/jira/site")
def select_jira_site(
    body: JiraSiteSelectionRequest, context: ContextDependency, user: AdminDependency
):
    return context.integrations.select_jira_site(body.selection_id, body.cloud_id, user)


@router.post("/jira/check")
def check_jira(context: ContextDependency, user: AdminDependency):
    return context.integrations.check_jira(user)


@router.delete("/jira", status_code=204)
def disconnect_jira(context: ContextDependency, user: AdminDependency) -> None:
    context.integrations.disconnect(IntegrationProvider.JIRA, user)
