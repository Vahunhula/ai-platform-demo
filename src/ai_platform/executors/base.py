"""Provider-neutral coding-agent executor contracts."""

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ai_platform.models import ModelSelection, TaskDefinition


class AgentActivityType(StrEnum):
    """Normalized activity kinds emitted by any agent provider."""

    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


class AgentActivity(BaseModel):
    """One provider-neutral, bounded activity record."""

    activity_type: AgentActivityType
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutorPreflight(BaseModel):
    """Non-secret executor and authentication information."""

    provider: str
    sdk_version: str
    authentication_method: str


class ExecutionRequest(BaseModel):
    """Inputs a coding executor receives for one implementation attempt."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    task: TaskDefinition
    selection: ModelSelection
    workspace_path: Path
    execution_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    previous_failure: str | None = None
    continuation: bool = False
    human_messages: list[str] = Field(default_factory=list)
    recent_agent_messages: list[str] = Field(default_factory=list)
    current_verification: str = "not_run"
    workspace_diff: str = ""
    human_workspace_changed: bool = False
    cancellation_requested: Callable[[], bool] | None = Field(default=None, exclude=True)


class ExecutionResult(BaseModel):
    """Provider-neutral result from one coding-agent attempt."""

    succeeded: bool
    summary: str
    activities: list[AgentActivity] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    error: str | None = None
    fatal: bool = False
    cancelled: bool = False


class AgentExecutorError(RuntimeError):
    """A safe, user-facing executor failure."""

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


class AgentAuthenticationError(AgentExecutorError):
    """Raised when no supported Claude authentication is available."""

    def __init__(self, message: str) -> None:
        super().__init__(message, fatal=True)


class AgentExecutor(Protocol):
    """Structural interface for current and future coding-agent providers."""

    def preflight(self) -> ExecutorPreflight:
        """Validate availability and report non-secret authentication details."""
        ...

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Attempt a task inside its assigned workspace."""
        ...


TaskExecutor = AgentExecutor
