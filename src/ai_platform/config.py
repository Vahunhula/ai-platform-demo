"""Environment-backed application configuration."""

import os
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


@dataclass(frozen=True, slots=True)
class Settings:
    """All runtime settings, with portable repository-relative defaults."""

    project_root: Path
    tasks_path: Path
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
    max_attempts: int

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "Settings":
        """Load supported settings without injecting arbitrary .env values globally."""

        root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        values: dict[str, Any] = {**dotenv_values(root / ".env"), **os.environ}

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

        return cls(
            project_root=root,
            tasks_path=root / "tasks.json",
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
            max_attempts=_positive_int(values, "AI_PLATFORM_MAX_ATTEMPTS", 2),
        )
