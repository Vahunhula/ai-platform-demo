"""Environment-backed application configuration."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values


def _resolve_path(value: str, project_root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else project_root / path


def _positive_int(values: dict[str, Any], name: str, default: int) -> int:
    raw_value = values.get(name, default)
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < 1:
        raise ValueError(f"{name} must be greater than zero")
    return value


_ORIGIN_PATTERN = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}


def _flag(values: dict[str, Any], name: str) -> bool:
    raw_value = str(values.get(name) or "").strip().lower()
    if raw_value in _TRUE_VALUES:
        return True
    if raw_value in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be one of 1/0, true/false, yes/no, on/off")


def _origins(values: dict[str, Any]) -> tuple[str, ...]:
    raw_value = str(values.get("AI_PLATFORM_ALLOWED_ORIGINS") or "").strip()
    origins = tuple(item.strip().rstrip("/") for item in raw_value.split(",") if item.strip())
    for origin in origins:
        if not _ORIGIN_PATTERN.fullmatch(origin):
            raise ValueError(f"AI_PLATFORM_ALLOWED_ORIGINS contains an invalid origin: {origin}")
    return origins


@dataclass(frozen=True, slots=True)
class Settings:
    """All runtime settings, with portable repository-relative defaults."""

    project_root: Path
    tasks_path: Path
    demo_repository: Path
    data_dir: Path
    workspace_root: Path
    db_path: Path
    checkpoint_db_path: Path
    cheap_model: str
    default_model: str
    strong_model: str
    anthropic_api_key: str | None
    executor: str
    agent_timeout_seconds: int
    agent_max_turns: int
    verification_timeout_seconds: int
    max_attempts_per_tier: int
    lock_heartbeat_seconds: int
    lock_stale_seconds: int
    # Whether the HTTP API process executes browser-initiated agent turns.
    enable_runner: bool = False
    # Web session lifetime and cookie policy (Demo 2 Phase 4 authentication).
    session_hours: int = 12
    cookie_secure: bool = False
    # Extra browser origins allowed to send mutations, besides the request's own host.
    allowed_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "Settings":
        """Load supported settings without injecting arbitrary .env values globally."""

        root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        values: dict[str, Any] = {**dotenv_values(root / ".env"), **os.environ}
        if "AI_PLATFORM_MAX_ATTEMPTS_PER_TIER" not in values:
            legacy_attempts = values.get("AI_PLATFORM_MAX_ATTEMPTS")
            if legacy_attempts not in (None, ""):
                values["AI_PLATFORM_MAX_ATTEMPTS_PER_TIER"] = legacy_attempts

        def setting(name: str, default: str) -> str:
            value = values.get(name, default)
            return str(value) if value not in (None, "") else default

        data_dir = _resolve_path(setting("AI_PLATFORM_DATA_DIR", "data"), root)
        workspace_root = _resolve_path(setting("AI_PLATFORM_WORKSPACE_ROOT", "workspaces"), root)
        db_path = _resolve_path(setting("AI_PLATFORM_DB_PATH", str(data_dir / "platform.db")), root)
        checkpoint_db_path = _resolve_path(
            setting("AI_PLATFORM_CHECKPOINT_DB_PATH", str(data_dir / "langgraph-checkpoints.db")),
            root,
        )

        heartbeat_seconds = _positive_int(values, "AI_PLATFORM_LOCK_HEARTBEAT_SECONDS", 5)
        stale_seconds = _positive_int(values, "AI_PLATFORM_LOCK_STALE_SECONDS", 60)
        if stale_seconds <= heartbeat_seconds:
            raise ValueError(
                "AI_PLATFORM_LOCK_STALE_SECONDS must be greater than "
                "AI_PLATFORM_LOCK_HEARTBEAT_SECONDS"
            )

        return cls(
            project_root=root,
            tasks_path=_resolve_path(setting("AI_PLATFORM_TASK_FILE", "tasks.json"), root),
            demo_repository=_resolve_path(
                setting("AI_PLATFORM_DEMO_REPO", "demo_repo"), root
            ),
            data_dir=data_dir,
            workspace_root=workspace_root,
            db_path=db_path,
            checkpoint_db_path=checkpoint_db_path,
            cheap_model=setting("AI_PLATFORM_CHEAP_MODEL", "haiku"),
            default_model=setting("AI_PLATFORM_DEFAULT_MODEL", "sonnet"),
            strong_model=setting("AI_PLATFORM_STRONG_MODEL", "opus"),
            anthropic_api_key=values.get("ANTHROPIC_API_KEY") or None,
            executor=setting("AI_PLATFORM_EXECUTOR", "claude"),
            agent_timeout_seconds=_positive_int(values, "AI_PLATFORM_AGENT_TIMEOUT_SECONDS", 300),
            agent_max_turns=_positive_int(values, "AI_PLATFORM_AGENT_MAX_TURNS", 8),
            verification_timeout_seconds=_positive_int(
                values, "AI_PLATFORM_VERIFICATION_TIMEOUT_SECONDS", 60
            ),
            max_attempts_per_tier=_positive_int(
                values, "AI_PLATFORM_MAX_ATTEMPTS_PER_TIER", 2
            ),
            lock_heartbeat_seconds=heartbeat_seconds,
            lock_stale_seconds=stale_seconds,
            enable_runner=_flag(values, "AI_PLATFORM_ENABLE_RUNNER"),
            session_hours=_positive_int(values, "AI_PLATFORM_SESSION_HOURS", 12),
            cookie_secure=_flag(values, "AI_PLATFORM_COOKIE_SECURE"),
            allowed_origins=_origins(values),
        )
