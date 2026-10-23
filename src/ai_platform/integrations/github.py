"""GitHub App installation authentication and connection lifecycle primitives."""

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from ai_platform.config import Settings
from ai_platform.integrations.http import JsonHttpClient, ProviderHttpError
from ai_platform.integrations.models import IntegrationConfigurationError, IntegrationError

_API = "https://api.github.com"
_API_VERSION = "2026-03-10"


@dataclass(frozen=True, slots=True)
class GitHubInstallation:
    installation_id: int
    account_id: str
    account_name: str
    account_type: str
    repository_selection: str


class GitHubClient:
    def __init__(self, settings: Settings, http: JsonHttpClient) -> None:
        self.settings = settings
        self.http = http

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.github_app_id
            and self.settings.github_app_slug
            and (self.settings.github_app_private_key or self.settings.github_app_private_key_path)
        )

    def installation_url(self, state: str) -> str:
        if not self.settings.github_app_slug:
            raise IntegrationConfigurationError("GitHub App configuration is incomplete.")
        from urllib.parse import quote

        return (
            f"https://github.com/apps/{quote(self.settings.github_app_slug, safe='-')}/"
            f"installations/new?state={quote(state, safe='')}"
        )

    def get_installation(self, installation_id: int) -> GitHubInstallation:
        response = self.http.request(
            "GET",
            f"{_API}/app/installations/{installation_id}",
            headers=self._headers(self._app_jwt()),
        )
        data = _object(response.data)
        account = _object(data.get("account"))
        try:
            return GitHubInstallation(
                installation_id=int(data["id"]),
                account_id=str(account["id"]),
                account_name=str(account.get("login") or account.get("name")),
                account_type=str(account.get("type") or "Unknown"),
                repository_selection=str(data.get("repository_selection") or "selected"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrationError("GitHub returned invalid installation metadata.") from error

    def installation_token(self, installation_id: int) -> tuple[str, str | None]:
        response = self.http.request(
            "POST",
            f"{_API}/app/installations/{installation_id}/access_tokens",
            headers=self._headers(self._app_jwt()),
            body={"permissions": {"metadata": "read"}},
        )
        data = _object(response.data)
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise IntegrationError("GitHub did not issue an installation token.")
        return token, data.get("expires_at") if isinstance(data.get("expires_at"), str) else None

    def repository_count(self, installation_token: str) -> int:
        total = 0
        page = 1
        while True:
            response = self.http.request(
                "GET",
                f"{_API}/installation/repositories?per_page=100&page={page}",
                headers=self._headers(installation_token),
            )
            data = _object(response.data)
            repositories = data.get("repositories")
            if not isinstance(repositories, list):
                raise IntegrationError("GitHub returned invalid repository metadata.")
            total += len(repositories)
            if len(repositories) < 100:
                return int(data.get("total_count", total))
            page += 1

    def validate(self, installation_id: int) -> tuple[GitHubInstallation, int]:
        installation = self.get_installation(installation_id)
        token, _expires = self.installation_token(installation_id)
        return installation, self.repository_count(token)

    def _app_jwt(self) -> str:
        if not self.settings.github_app_id:
            raise IntegrationConfigurationError("GitHub App configuration is incomplete.")
        private_key = self._private_key()
        now = int(time.time())
        header = _b64({"alg": "RS256", "typ": "JWT"})
        payload = _b64({"iat": now - 60, "exp": now + 540, "iss": self.settings.github_app_id})
        signing_input = f"{header}.{payload}".encode()
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{payload}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

    def _private_key(self):  # noqa: ANN201
        raw = self.settings.github_app_private_key
        path = self.settings.github_app_private_key_path
        if raw:
            pem = raw.replace("\\n", "\n").encode()
        elif path:
            try:
                pem = Path(path).read_bytes()
            except OSError as error:
                raise IntegrationConfigurationError(
                    "GitHub App private key cannot be read."
                ) from error
        else:
            raise IntegrationConfigurationError("GitHub App private key is not configured.")
        try:
            return serialization.load_pem_private_key(pem, password=None)
        except (TypeError, ValueError) as error:
            raise IntegrationConfigurationError("GitHub App private key is invalid.") from error

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": _API_VERSION,
            "User-Agent": "ai-platform-demo",
        }


def is_missing_or_revoked(error: Exception) -> bool:
    return isinstance(error, ProviderHttpError) and error.status in {401, 403, 404}


def _b64(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IntegrationError("Provider returned an invalid response.")
    return value
