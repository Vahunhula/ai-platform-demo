"""Core task and model-routing domain models."""

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, Field, field_validator


class TaskDifficulty(StrEnum):
    """Difficulty declared by a task definition."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskStatus(StrEnum):
    """Lifecycle shared by automation and collaborating humans."""

    READY = "ready"
    ANALYZING = "analyzing"
    IMPLEMENTING = "implementing"
    VERIFYING = "verifying"
    WAITING_FOR_HUMAN = "waiting_for_human"
    PAUSED_BY_HUMAN = "paused_by_human"
    COMPLETED = "completed"
    FAILED = "failed"


class ExecutionKind(StrEnum):
    """Mutually exclusive workspace writers."""

    AGENT = "agent"
    HUMAN_SHELL = "human_shell"


class VerificationStatus(StrEnum):
    """Latest deterministic verification result for a task."""

    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"


class VerificationType(StrEnum):
    """Supported structured verification runners."""

    PYTEST = "pytest"


class VerificationConfig(BaseModel):
    """Validated verification configuration; never an arbitrary shell string."""

    type: VerificationType
    targets: list[str] = Field(min_length=1)

    @field_validator("targets")
    @classmethod
    def validate_targets(cls, targets: list[str]) -> list[str]:
        """Restrict targets to safe relative Python test files."""

        for target in targets:
            path = PurePosixPath(target.replace("\\", "/"))
            if (
                path.is_absolute()
                or ".." in path.parts
                or target.startswith("-")
                or path.suffix != ".py"
            ):
                raise ValueError(f"Unsafe pytest target: {target!r}")
        return targets


class TaskDefinition(BaseModel):
    """A task loaded from the source task file."""

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    difficulty: TaskDifficulty
    acceptance_criteria: list[str] = Field(min_length=1)
    verification: VerificationConfig


class ModelTier(StrEnum):
    """Provider-independent model capability tier."""

    CHEAP = "cheap"
    DEFAULT = "default"
    STRONG = "strong"


class ModelSelection(BaseModel):
    """The model tier and configured provider model chosen for a task."""

    tier: ModelTier
    model: str = Field(min_length=1)
    reason: str


class TaskRecord(BaseModel):
    """Persisted runtime state for a task."""

    task_id: str
    title: str
    difficulty: TaskDifficulty
    status: TaskStatus
    selected_tier: ModelTier | None = None
    selected_model: str | None = None
    attempt: int = 0
    verification_status: VerificationStatus = VerificationStatus.NOT_RUN
    workspace_path: str | None = None
    active_execution: ExecutionKind | None = None
    execution_owner: str | None = None
    execution_id: str | None = None
    execution_actor_id: str | None = None
    execution_pid: int | None = None
    execution_hostname: str | None = None
    execution_started_at: datetime | None = None
    execution_heartbeat_at: datetime | None = None
    pause_requested: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def agent_running(self) -> bool:
        """Return whether an agent currently owns the workspace lock."""

        return self.active_execution is ExecutionKind.AGENT

    @property
    def shell_running(self) -> bool:
        """Return whether a human shell currently owns the workspace lock."""

        return self.active_execution is ExecutionKind.HUMAN_SHELL
