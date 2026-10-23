"""Provider-neutral integration domain values."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class IntegrationProvider(StrEnum):
    GITHUB = "GITHUB"
    JIRA = "JIRA"


class ConnectionStatus(StrEnum):
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"


@dataclass(frozen=True, slots=True)
class IntegrationConnection:
    id: str
    provider: IntegrationProvider
    status: ConnectionStatus
    display_name: str
    external_account_id: str | None
    external_account_name: str | None
    external_site_id: str | None
    external_site_url: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_by: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_checked_at: datetime | None = None
    last_error: str | None = None


class IntegrationError(RuntimeError):
    """A deliberately browser-safe integration error."""


class IntegrationConfigurationError(IntegrationError):
    pass


class IntegrationAuthenticationError(IntegrationError):
    pass


class OAuthStateError(IntegrationError):
    pass
