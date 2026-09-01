"""Duplicated display-name behavior for DEMO-3."""


def profile_display_name(first_name: str, last_name: str) -> str:
    """Format a name for a user profile."""

    return f"{first_name.strip()} {last_name.strip()}"


def audit_display_name(first_name: str, last_name: str) -> str:
    """Format a name for the audit log through a separate, inconsistent path."""

    return f"{last_name.strip()}, {first_name.strip()}"
