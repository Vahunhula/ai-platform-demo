"""Core task and model-routing domain models."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class TaskDifficulty(StrEnum):
    """Difficulty declared by a task definition."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskStatus(StrEnum):
    """Small lifecycle used by the foundation workflow."""

    READY = "ready"
    ANALYZING = "analyzing"
    IMPLEMENTING = "implementing"
    VERIFYING = "verifying"
    WAITING_FOR_HUMAN = "waiting_for_human"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskDefinition(BaseModel):
    """A task loaded from the source task file."""

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    difficulty: TaskDifficulty
    acceptance_criteria: list[str] = Field(min_length=1)


class ModelTier(StrEnum):
    """Provider-independent model capability tier."""

    CHEAP = "cheap"
    DEFAULT = "default"
    STRONG = "strong"


class ModelSelection(BaseModel):
    """The model tier and configured provider model chosen for a task."""

    tier: ModelTier
    model: str
    reason: str


class TaskRecord(BaseModel):
    """Persisted runtime state for a task."""

    task_id: str
    title: str
    difficulty: TaskDifficulty
    status: TaskStatus
    selected_tier: ModelTier | None = None
    selected_model: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
