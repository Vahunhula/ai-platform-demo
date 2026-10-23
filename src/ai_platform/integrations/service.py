"""Provider-neutral integration lifecycle service."""

import hashlib
import hmac
import json
import secrets
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from ai_platform.auth import AuthenticatedUser, Role
from ai_platform.config import Settings
from ai_platform.integrations.crypto import CredentialCipher
from ai_platform.integrations.github import GitHubClient, is_missing_or_revoked
from ai_platform.integrations.http import JsonHttpClient, ProviderHttpError, UrllibJsonClient
from ai_platform.integrations.jira import JiraClient
from ai_platform.integrations.models import (
    ConnectionStatus,
    IntegrationAuthenticationError,
    IntegrationConfigurationError,
    IntegrationConnection,
    IntegrationError,
    IntegrationProvider,
)
from ai_platform.integrations.storage import IntegrationStore
from ai_platform.storage import SQLiteStorage

_STATE_LIFETIME = timedelta(minutes=10)
_SELECTION_LIFETIME = timedelta(minutes=10)


class IntegrationService:
    """Own authentication, credentials, health, disconnect, webhook, and audit."""

    def __init__(
        self,
        settings: Settings,
        storage: SQLiteStorage,
        *,
        http: JsonHttpClient | None = None,
    ) -> None:
        self.settings = settings
        self.store = IntegrationStore(storage)
        self.store.initialize()
        self.cipher = CredentialCipher(settings.integration_encryption_key)
        if self.store.encrypted_credentials_exist() and not self.cipher.available:
            raise IntegrationConfigurationError(
                "Encrypted integration credentials exist but the encryption key is unavailable."
            )
        # Detect a wrong key at startup instead of waiting for the first refresh.
        for envelope, context in self.store.encrypted_envelopes():
            self.cipher.decrypt(envelope, context=context)
        provider_http = http or UrllibJsonClient()
        self.github = GitHubClient(settings, provider_http)
        self.jira = JiraClient(settings, provider_http)
        self._jira_refresh_lock = threading.Lock()
        self._refresh_owner = f"refresh-{uuid4().hex}"

    def list_public(self, actor: AuthenticatedUser) -> list[dict[str, Any]]:
        self._require_admin(actor)
        existing = {item.provider: item for item in self.store.list_connections()}
        return [
            self._public(IntegrationProvider.GITHUB, existing.get(IntegrationProvider.GITHUB)),
            self._public(IntegrationProvider.JIRA, existing.get(IntegrationProvider.JIRA)),
        ]

    def oauth_user(self, provider: IntegrationProvider, state: str) -> str:
        """Return the platform user that initiated a valid callback state."""

        return self.store.oauth_state_user(state, provider)

    def get_public(self, provider: IntegrationProvider, actor: AuthenticatedUser) -> dict[str, Any]:
        self._require_admin(actor)
        return self._public(provider, self.store.get_connection(provider))

    def start_github(self, actor: AuthenticatedUser) -> dict[str, str]:
        self._require_admin(actor)
        if not self.github.configured:
            raise IntegrationConfigurationError("GitHub App configuration is incomplete.")
        state = secrets.token_urlsafe(32)
        self.store.create_oauth_state(
            state, IntegrationProvider.GITHUB, actor.user_id, datetime.now(UTC) + _STATE_LIFETIME
        )
        self.store.audit("INTEGRATION_CONNECT_STARTED", IntegrationProvider.GITHUB, actor.username)
        return {"authorization_url": self.github.installation_url(state)}

    def complete_github(
        self, installation_id: int, state: str, actor: AuthenticatedUser
    ) -> dict[str, Any]:
        self._require_admin(actor)
        self.store.consume_oauth_state(state, IntegrationProvider.GITHUB, actor.user_id)
        installation, repository_count = self.github.validate(installation_id)
        connection = self.store.save_connection(
            IntegrationProvider.GITHUB,
            created_by=actor.username,
            display_name=installation.account_name,
            external_account_id=installation.account_id,
            external_account_name=installation.account_name,
            metadata={
                "installation_id": installation.installation_id,
                "account_type": installation.account_type,
                "repository_selection": installation.repository_selection,
                "repository_count": repository_count,
            },
        )
        self.store.audit(
            "INTEGRATION_CONNECTED",
            IntegrationProvider.GITHUB,
            actor.username,
            {"external_account_id": installation.account_id, "repository_count": repository_count},
        )
        return self._public(IntegrationProvider.GITHUB, connection)

    def check_github(self, actor: AuthenticatedUser) -> dict[str, Any]:
        self._require_admin(actor)
        connection = self._connected(IntegrationProvider.GITHUB)
        try:
            installation_id = int(connection.metadata["installation_id"])
            installation, count = self.github.validate(installation_id)
            updated = self.store.update_health(
                IntegrationProvider.GITHUB,
                status=ConnectionStatus.CONNECTED,
                safe_error=None,
                metadata={"repository_count": count},
                account_name=installation.account_name,
            )
            healthy = True
            safe_error = None
        except Exception as error:
            reauth = is_missing_or_revoked(error)
            safe_error = (
                "GitHub App installation is missing or no longer authorized. Reconnect GitHub."
                if reauth
                else "GitHub connection check failed. Try again."
            )
            updated = self.store.update_health(
                IntegrationProvider.GITHUB,
                status=(ConnectionStatus.REAUTH_REQUIRED if reauth else ConnectionStatus.DEGRADED),
                safe_error=safe_error,
            )
            healthy = False
        self._audit_health(updated, actor.username, healthy)
        return self._health(updated, healthy, safe_error)

    def start_jira(self, actor: AuthenticatedUser) -> dict[str, str]:
        self._require_admin(actor)
        if not self.jira.configured or not self.cipher.available:
            raise IntegrationConfigurationError(
                "Jira OAuth and integration encryption configuration are required."
            )
        state = secrets.token_urlsafe(32)
        self.store.create_oauth_state(
            state, IntegrationProvider.JIRA, actor.user_id, datetime.now(UTC) + _STATE_LIFETIME
        )
        self.store.audit("INTEGRATION_CONNECT_STARTED", IntegrationProvider.JIRA, actor.username)
        return {"authorization_url": self.jira.authorization_url(state)}

    def complete_jira(self, code: str, state: str, actor: AuthenticatedUser) -> dict[str, Any]:
        self._require_admin(actor)
        self.store.consume_oauth_state(state, IntegrationProvider.JIRA, actor.user_id)
        credentials = self.jira.exchange_code(code)
        resources = self.jira.resources(credentials["access_token"])
        profile = self.jira.profile(credentials["access_token"])
        credentials["account"] = profile
        if not resources:
            raise IntegrationAuthenticationError("No accessible Jira Cloud sites were found.")
        if len(resources) == 1:
            connection = self._save_jira(actor, credentials, resources[0])
            return {
                "selection_required": False,
                "connection": self._public(IntegrationProvider.JIRA, connection),
            }
        envelope = self.cipher.encrypt(credentials, context="JIRA_PENDING")
        selection_id = self.store.create_site_selection(
            actor.user_id,
            envelope,
            resources,
            datetime.now(UTC) + _SELECTION_LIFETIME,
        )
        return {
            "selection_required": True,
            "selection_id": selection_id,
            "sites": [_safe_site(item) for item in resources],
        }

    def pending_jira_sites(self, selection_id: str, actor: AuthenticatedUser) -> dict[str, Any]:
        self._require_admin(actor)
        _envelope, resources = self.store.site_selection(selection_id, actor.user_id)
        return {"selection_id": selection_id, "sites": [_safe_site(item) for item in resources]}

    def select_jira_site(
        self, selection_id: str, cloud_id: str, actor: AuthenticatedUser
    ) -> dict[str, Any]:
        self._require_admin(actor)
        envelope, resources = self.store.site_selection(selection_id, actor.user_id)
        credentials = self.cipher.decrypt(envelope, context="JIRA_PENDING")
        site = next((item for item in resources if item["id"] == cloud_id), None)
        if site is None:
            raise IntegrationError(
                "The selected Jira site is not available for this authorization."
            )
        self.jira.validate_jira(credentials["access_token"], cloud_id)
        connection = self._save_jira(actor, credentials, site)
        self.store.consume_site_selection(selection_id, actor.user_id)
        return self._public(IntegrationProvider.JIRA, connection)

    def check_jira(self, actor: AuthenticatedUser) -> dict[str, Any]:
        self._require_admin(actor)
        connection = self._connected(IntegrationProvider.JIRA)
        try:
            credentials = self._jira_credentials(connection)
            resources = self.jira.resources(credentials["access_token"])
            site = next(
                (item for item in resources if item["id"] == connection.external_site_id), None
            )
            if site is None:
                raise IntegrationAuthenticationError(
                    "The selected Jira site is no longer accessible. Reconnect Jira."
                )
            account = self.jira.profile(credentials["access_token"])
            self.jira.validate_jira(credentials["access_token"], site["id"])
            updated = self.store.update_health(
                IntegrationProvider.JIRA,
                status=ConnectionStatus.CONNECTED,
                safe_error=None,
                metadata={"scopes": credentials.get("scopes", site.get("scopes", []))},
                account_name=account["name"],
            )
            healthy = True
            safe_error = None
        except IntegrationAuthenticationError as error:
            safe_error = str(error)
            updated = self.store.update_health(
                IntegrationProvider.JIRA,
                status=ConnectionStatus.REAUTH_REQUIRED,
                safe_error=safe_error,
            )
            healthy = False
        except ProviderHttpError as error:
            reauth = error.status in {400, 401, 403}
            safe_error = (
                "Jira authorization is invalid or revoked. Reconnect Jira."
                if reauth
                else "Jira connection check failed. Try again."
            )
            updated = self.store.update_health(
                IntegrationProvider.JIRA,
                status=(ConnectionStatus.REAUTH_REQUIRED if reauth else ConnectionStatus.DEGRADED),
                safe_error=safe_error,
            )
            healthy = False
        self._audit_health(updated, actor.username, healthy)
        return self._health(updated, healthy, safe_error)

    def disconnect(self, provider: IntegrationProvider, actor: AuthenticatedUser) -> None:
        self._require_admin(actor)
        self.store.disconnect(provider)
        self.store.audit("INTEGRATION_DISCONNECTED", provider, actor.username)

    def github_webhook(self, body: bytes, signature: str | None, delivery: str, event: str) -> bool:
        secret = self.settings.github_app_webhook_secret
        if not secret:
            raise IntegrationConfigurationError("GitHub webhook verification is not configured.")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(expected, signature):
            raise IntegrationAuthenticationError("Invalid GitHub webhook signature.")
        if not delivery:
            raise IntegrationError("GitHub webhook delivery ID is required.")
        if not self.store.record_delivery(delivery, event):
            return False
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise IntegrationError("GitHub webhook payload is invalid.") from error
        if not isinstance(payload, dict):
            raise IntegrationError("GitHub webhook payload is invalid.")
        self.store.audit(
            "INTEGRATION_WEBHOOK_RECEIVED",
            IntegrationProvider.GITHUB,
            "github-webhook",
            {"event": event, "action": payload.get("action")},
        )
        installation_data = payload.get("installation")
        installation_id = (
            installation_data.get("id") if isinstance(installation_data, dict) else None
        )
        current = self.store.get_connection(IntegrationProvider.GITHUB)
        if event == "installation" and payload.get("action") in {"deleted", "suspend"}:
            if current and current.metadata.get("installation_id") == installation_id:
                self.store.update_health(
                    IntegrationProvider.GITHUB,
                    status=ConnectionStatus.REAUTH_REQUIRED,
                    safe_error=(
                        "GitHub App installation was removed or suspended. Reconnect GitHub."
                    ),
                )
                self.store.audit(
                    "INTEGRATION_REAUTH_REQUIRED",
                    IntegrationProvider.GITHUB,
                    "github-webhook",
                    {"external_account_id": current.external_account_id},
                )
        elif event in {"installation", "installation_repositories"} and installation_id:
            # Re-fetch authenticated installation/repository data; do not trust payload metadata.
            installation, count = self.github.validate(int(installation_id))
            if current and current.metadata.get("installation_id") == installation_id:
                self.store.update_health(
                    IntegrationProvider.GITHUB,
                    status=ConnectionStatus.CONNECTED,
                    safe_error=None,
                    metadata={"repository_count": count},
                    account_name=installation.account_name,
                )
                self.store.audit(
                    "INTEGRATION_REPOSITORY_ACCESS_CHANGED",
                    IntegrationProvider.GITHUB,
                    "github-webhook",
                    {"repository_count": count},
                )
        return True

    def _save_jira(
        self, actor: AuthenticatedUser, credentials: dict[str, Any], site: dict[str, Any]
    ) -> IntegrationConnection:
        account = credentials.get("account")
        if not isinstance(account, dict):
            account = self.jira.profile(credentials["access_token"])
        self.jira.validate_jira(credentials["access_token"], site["id"])
        envelope = self.cipher.encrypt(credentials, context="JIRA")
        connection = self.store.save_connection(
            IntegrationProvider.JIRA,
            created_by=actor.username,
            display_name=str(site["name"]),
            external_account_id=str(account["account_id"]),
            external_account_name=str(account["name"]),
            external_site_id=str(site["id"]),
            external_site_url=str(site["url"]),
            metadata={"scopes": credentials.get("scopes", site.get("scopes", []))},
            encrypted_credentials=envelope,
        )
        self.store.audit(
            "INTEGRATION_CONNECTED",
            IntegrationProvider.JIRA,
            actor.username,
            {"external_account_id": account["account_id"], "external_site_id": site["id"]},
        )
        return connection

    def _jira_credentials(self, connection: IntegrationConnection) -> dict[str, Any]:
        with self._jira_refresh_lock:
            try:
                self.store.acquire_refresh_lock(connection.id, self._refresh_owner)
            except TimeoutError as error:
                raise IntegrationError(
                    "Jira credential refresh is busy. Try the health check again."
                ) from error
            try:
                # Reload after taking the cross-process lease: another worker may
                # have rotated while this worker waited.
                stored = self.store.credentials(connection.id)
                if stored is None:
                    raise IntegrationAuthenticationError(
                        "Jira credentials are unavailable. Reconnect Jira."
                    )
                envelope, revision = stored
                credentials = self.cipher.decrypt(envelope, context="JIRA")
                if not self.jira.access_expired(credentials):
                    return credentials
                refreshed = self.jira.refresh(str(credentials.get("refresh_token", "")))
                refreshed["account"] = credentials.get("account", {})
                new_envelope = self.cipher.encrypt(refreshed, context="JIRA")
                if self.store.rotate_credentials(connection.id, revision, new_envelope):
                    return refreshed
                # Defensive CAS fallback for a lease expiry or manual operation.
                latest = self.store.credentials(connection.id)
                if latest is None:
                    raise IntegrationAuthenticationError("Jira was disconnected during refresh.")
                return self.cipher.decrypt(latest[0], context="JIRA")
            finally:
                self.store.release_refresh_lock(connection.id, self._refresh_owner)

    def _connected(self, provider: IntegrationProvider) -> IntegrationConnection:
        connection = self.store.get_connection(provider)
        if connection is None or connection.status is ConnectionStatus.DISCONNECTED:
            raise IntegrationError(f"{provider.value.title()} is not connected.")
        return connection

    def _provider_configured(self, provider: IntegrationProvider) -> bool:
        if provider is IntegrationProvider.GITHUB:
            return self.github.configured
        return self.jira.configured and self.cipher.available

    def _public(
        self, provider: IntegrationProvider, connection: IntegrationConnection | None
    ) -> dict[str, Any]:
        configured = self._provider_configured(provider)
        if connection is None:
            return {
                "provider": provider.value,
                "configured": configured,
                "status": "NOT_CONNECTED" if configured else "NOT_CONFIGURED",
                "display_name": None,
                "account": None,
                "site": None,
                "metadata": {},
                "last_checked_at": None,
                "safe_error": None,
            }
        metadata = {
            key: value
            for key, value in connection.metadata.items()
            if key
            in {
                "installation_id",
                "account_type",
                "repository_selection",
                "repository_count",
                "scopes",
            }
        }
        return {
            "provider": provider.value,
            "configured": configured,
            "status": connection.status.value,
            "display_name": connection.display_name,
            "account": {
                "id": connection.external_account_id,
                "name": connection.external_account_name,
            },
            "site": (
                {
                    "id": connection.external_site_id,
                    "url": connection.external_site_url,
                    "name": connection.display_name,
                }
                if connection.external_site_id
                else None
            ),
            "metadata": metadata,
            "last_checked_at": (
                connection.last_checked_at.isoformat() if connection.last_checked_at else None
            ),
            "safe_error": connection.last_error,
        }

    def _health(
        self, connection: IntegrationConnection, healthy: bool, safe_error: str | None
    ) -> dict[str, Any]:
        public = self._public(connection.provider, connection)
        return {
            "healthy": healthy,
            "checked_at": public["last_checked_at"],
            "account": public["account"],
            "site": public["site"],
            "repository_count": public["metadata"].get("repository_count"),
            "scopes": public["metadata"].get("scopes", []),
            "safe_error": safe_error,
            "status": public["status"],
        }

    def _audit_health(
        self, connection: IntegrationConnection, actor_id: str, healthy: bool
    ) -> None:
        self.store.audit(
            "INTEGRATION_HEALTH_CHECKED",
            connection.provider,
            actor_id,
            {"healthy": healthy, "status": connection.status.value},
        )
        if connection.status is ConnectionStatus.REAUTH_REQUIRED:
            self.store.audit("INTEGRATION_REAUTH_REQUIRED", connection.provider, actor_id)

    @staticmethod
    def _require_admin(actor: AuthenticatedUser) -> None:
        if actor.role is not Role.ADMIN:
            raise IntegrationError("Administrator access is required to manage integrations.")


def _safe_site(site: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(site["id"]),
        "name": str(site["name"]),
        "url": str(site["url"]),
        "scopes": [str(item) for item in site.get("scopes", [])],
    }
