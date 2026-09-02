"""Read-only Claude authentication inspection shared by preflight and doctor."""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ai_platform.config import Settings


class ClaudeAuthState(StrEnum):
    """Confidence level available without making a paid model request."""

    AUTHENTICATED = "authenticated"
    CONFIGURED = "configured"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ClaudeAuthStatus:
    """Sanitized result of inspecting supported local credential sources."""

    state: ClaudeAuthState
    method: str
    detail: str
    cli_path: Path | None


def inspect_claude_auth(settings: Settings) -> ClaudeAuthStatus:
    """Inspect credentials without exposing secrets or making a model request."""

    cli_path_value = shutil.which("claude")
    cli_path = Path(cli_path_value) if cli_path_value else None
    cloud_methods = (
        ("CLAUDE_CODE_USE_BEDROCK", "Amazon Bedrock environment"),
        ("CLAUDE_CODE_USE_VERTEX", "Google Vertex environment"),
        ("CLAUDE_CODE_USE_FOUNDRY", "Microsoft Foundry environment"),
    )
    for variable, label in cloud_methods:
        if os.getenv(variable):
            return _configured(label, cli_path)
    if os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return _configured("ANTHROPIC_AUTH_TOKEN environment", cli_path)
    if settings.anthropic_api_key:
        return _configured("ANTHROPIC_API_KEY environment/.env", cli_path)
    if os.getenv("CLAUDE_CODE_OAUTH_TOKEN"):
        return _configured("CLAUDE_CODE_OAUTH_TOKEN environment", cli_path)

    if cli_path is None:
        return ClaudeAuthStatus(
            ClaudeAuthState.MISSING,
            "none",
            "Claude CLI was not found and no supported credential environment is configured",
            None,
        )
    try:
        completed = subprocess.run(
            [str(cli_path), "auth", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return ClaudeAuthStatus(
            ClaudeAuthState.UNKNOWN,
            "unknown",
            f"Claude auth status could not be inspected: {type(error).__name__}",
            cli_path,
        )
    try:
        status = json.loads(completed.stdout) if completed.stdout else {}
    except json.JSONDecodeError:
        return ClaudeAuthStatus(
            ClaudeAuthState.UNKNOWN,
            "unknown",
            "Claude CLI returned an unreadable authentication status",
            cli_path,
        )
    if completed.returncode == 0 and status.get("loggedIn") is True:
        method = str(status.get("authMethod") or "Claude Code login")
        plan = status.get("subscriptionType")
        label = f"Claude Code {method}" + (f" ({plan})" if plan else "")
        return ClaudeAuthStatus(
            ClaudeAuthState.AUTHENTICATED,
            label,
            "Claude CLI reports a logged-in account",
            cli_path,
        )
    if status.get("loggedIn") is False:
        return ClaudeAuthStatus(
            ClaudeAuthState.MISSING,
            str(status.get("authMethod") or "none"),
            "Claude CLI reports logged out; authentication may be missing or expired",
            cli_path,
        )
    return ClaudeAuthStatus(
        ClaudeAuthState.UNKNOWN,
        "unknown",
        f"Claude auth status exited with code {completed.returncode}",
        cli_path,
    )


def _configured(method: str, cli_path: Path | None) -> ClaudeAuthStatus:
    return ClaudeAuthStatus(
        ClaudeAuthState.CONFIGURED,
        method,
        "Credentials are configured but are not validated without a model request",
        cli_path,
    )
