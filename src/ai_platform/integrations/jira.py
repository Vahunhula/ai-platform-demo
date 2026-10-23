"""Atlassian OAuth 2.0 (3LO) and Jira Cloud provider operations."""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from ai_platform.config import Settings
from ai_platform.integrations.http import JsonHttpClient, ProviderHttpError
from ai_platform.integrations.models import (
    IntegrationAuthenticationError,
    IntegrationConfigurationError,
    IntegrationError,
)

AUTH_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
PROFILE_URL = "https://api.atlassian.com/me"
JIRA_SCOPES = ("read:me", "read:jira-user", "offline_access")


class JiraClient:
    def __init__(self, settings: Settings, http: JsonHttpClient) -> None:
        self.settings = settings
        self.http = http

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.jira_oauth_client_id
            and self.settings.jira_oauth_client_secret
            and self.settings.jira_oauth_redirect_uri
        )

    def authorization_url(self, state: str) -> str:
        self._require_configuration()
        parameters = {
            "audience": "api.atlassian.com",
            "client_id": self.settings.jira_oauth_client_id,
            "scope": " ".join(JIRA_SCOPES),
            "redirect_uri": self.settings.jira_oauth_redirect_uri,
            "state": state,
            "response_type": "code",
            "prompt": "consent",
        }
        return f"{AUTH_URL}?{urlencode(parameters)}"

    def exchange_code(self, code: str) -> dict[str, Any]:
        self._require_configuration()
        response = self.http.request(
            "POST",
            TOKEN_URL,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            body={
                "grant_type": "authorization_code",
                "client_id": self.settings.jira_oauth_client_id,
                "client_secret": self.settings.jira_oauth_client_secret,
                "code": code,
                "redirect_uri": self.settings.jira_oauth_redirect_uri,
            },
        )
        return self._token_payload(response.data)

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        self._require_configuration()
        try:
            response = self.http.request(
                "POST",
                TOKEN_URL,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                body={
                    "grant_type": "refresh_token",
                    "client_id": self.settings.jira_oauth_client_id,
                    "client_secret": self.settings.jira_oauth_client_secret,
                    "refresh_token": refresh_token,
                },
            )
        except ProviderHttpError as error:
            if error.status in {400, 401, 403}:
                raise IntegrationAuthenticationError(
                    "Jira authorization has expired or was revoked. Reconnect Jira."
                ) from None
            raise
        return self._token_payload(response.data)

    def resources(self, access_token: str) -> list[dict[str, Any]]:
        response = self.http.request("GET", RESOURCES_URL, headers=self._headers(access_token))
        if not isinstance(response.data, list):
            raise IntegrationError("Atlassian returned invalid site metadata.")
        resources = []
        for item in response.data:
            if not isinstance(item, dict) or not all(key in item for key in ("id", "name", "url")):
                continue
            scopes = [str(scope) for scope in item.get("scopes", [])]
            if not any("jira" in scope for scope in scopes):
                continue
            resources.append(
                {
                    "id": str(item["id"]),
                    "name": str(item["name"]),
                    "url": str(item["url"]),
                    "scopes": scopes,
                }
            )
        return resources

    def profile(self, access_token: str) -> dict[str, str]:
        response = self.http.request("GET", PROFILE_URL, headers=self._headers(access_token))
        if not isinstance(response.data, dict) or "account_id" not in response.data:
            raise IntegrationError("Atlassian returned invalid account metadata.")
        return {
            "account_id": str(response.data["account_id"]),
            "name": str(
                response.data.get("name") or response.data.get("nickname") or "Atlassian user"
            ),
        }

    def validate_jira(self, access_token: str, cloud_id: str) -> dict[str, Any]:
        response = self.http.request(
            "GET",
            f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/myself",
            headers=self._headers(access_token),
        )
        if not isinstance(response.data, dict):
            raise IntegrationError("Jira returned invalid account metadata.")
        return response.data

    @staticmethod
    def access_expired(credentials: dict[str, Any]) -> bool:
        try:
            expires_at = datetime.fromisoformat(str(credentials["expires_at"]))
        except (KeyError, TypeError, ValueError):
            return True
        return expires_at <= datetime.now(UTC) + timedelta(seconds=30)

    def _token_payload(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise IntegrationError("Atlassian returned an invalid token response.")
        access = value.get("access_token")
        refresh = value.get("refresh_token")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise IntegrationError("Atlassian did not return durable authorization credentials.")
        try:
            expires_in = int(value.get("expires_in", 3600))
        except (TypeError, ValueError) as error:
            raise IntegrationError("Atlassian returned an invalid token expiry.") from error
        scope = value.get("scope", " ".join(JIRA_SCOPES))
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat(),
            "scopes": str(scope).split(),
        }

    def _require_configuration(self) -> None:
        if not self.configured:
            raise IntegrationConfigurationError("Jira OAuth configuration is incomplete.")

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {"Accept": "application/json", "Authorization": f"Bearer {token}"}
