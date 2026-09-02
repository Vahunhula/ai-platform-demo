"""Human identity resolution for local collaborative development."""

import getpass
import os
from typing import Protocol

from pydantic import BaseModel, Field


class HumanIdentity(BaseModel):
    """A stable actor identifier and its human-facing label."""

    actor_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)


class IdentityProvider(Protocol):
    """Replaceable source of the current authenticated human."""

    def get_current_user(self) -> HumanIdentity:
        """Resolve the human operating the current client."""


class LocalIdentityProvider:
    """Resolve development identity from an override or the operating system."""

    def get_current_user(self) -> HumanIdentity:
        """Prefer AI_PLATFORM_USER, falling back to the OS account username."""

        username = os.getenv("AI_PLATFORM_USER") or getpass.getuser()
        username = username.strip()
        if not username or any(character in username for character in "\r\n\0"):
            raise ValueError("The current username must be a non-empty single-line value")
        return HumanIdentity(actor_id=username, display_name=username)
