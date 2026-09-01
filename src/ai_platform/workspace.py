"""Replaceable task workspace abstraction."""

import re
import shutil
from abc import ABC, abstractmethod
from pathlib import Path

_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class WorkspaceProvider(ABC):
    """Lifecycle contract for task-owned workspaces."""

    @abstractmethod
    def create(self, task_id: str) -> Path:
        """Create a workspace if necessary and return its path."""

    @abstractmethod
    def get_path(self, task_id: str) -> Path:
        """Return the path assigned to a task."""

    @abstractmethod
    def destroy(self, task_id: str) -> None:
        """Destroy a task workspace."""


class LocalWorkspaceProvider(WorkspaceProvider):
    """Store each workspace in a local directory below a configured root."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def create(self, task_id: str) -> Path:
        workspace = self.get_path(task_id)
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def get_path(self, task_id: str) -> Path:
        if not _SAFE_TASK_ID.fullmatch(task_id):
            raise ValueError(f"Unsafe task ID for workspace path: {task_id!r}")
        return self.root / task_id

    def destroy(self, task_id: str) -> None:
        workspace = self.get_path(task_id)
        if workspace.exists():
            shutil.rmtree(workspace)
