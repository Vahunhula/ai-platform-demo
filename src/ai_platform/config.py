"""Environment-backed application configuration."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _resolve_path(value: str, project_root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else project_root / path


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

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "Settings":
        """Load settings from the environment and an optional local .env file."""

        root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        load_dotenv(root / ".env")

        data_dir = _resolve_path(os.getenv("AI_PLATFORM_DATA_DIR", "data"), root)
        workspace_root = _resolve_path(os.getenv("AI_PLATFORM_WORKSPACE_ROOT", "workspaces"), root)
        db_path = _resolve_path(
            os.getenv("AI_PLATFORM_DB_PATH", str(data_dir / "platform.db")), root
        )
        checkpoint_db_path = _resolve_path(
            os.getenv("AI_PLATFORM_CHECKPOINT_DB_PATH", str(data_dir / "langgraph-checkpoints.db")),
            root,
        )

        return cls(
            project_root=root,
            tasks_path=root / "tasks.json",
            data_dir=data_dir,
            workspace_root=workspace_root,
            db_path=db_path,
            checkpoint_db_path=checkpoint_db_path,
            cheap_model=os.getenv("AI_PLATFORM_CHEAP_MODEL", "claude-cheap-not-configured"),
            default_model=os.getenv("AI_PLATFORM_DEFAULT_MODEL", "claude-default-not-configured"),
            strong_model=os.getenv("AI_PLATFORM_STRONG_MODEL", "claude-strong-not-configured"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        )
