"""Provider-neutral executor contract; no Claude calls exist in Demo 1 foundation."""

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from ai_platform.models import ModelSelection, TaskDefinition


class ExecutionRequest(BaseModel):
    """Inputs a future coding executor will receive."""

    task: TaskDefinition
    selection: ModelSelection
    workspace_path: Path


class ExecutionResult(BaseModel):
    """Minimal provider-neutral result from a future executor."""

    succeeded: bool
    summary: str


class TaskExecutor(Protocol):
    """Structural interface to be implemented by the future Claude executor."""

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Attempt a task inside its assigned workspace."""
        ...
