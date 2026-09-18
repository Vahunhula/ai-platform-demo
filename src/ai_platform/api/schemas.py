"""Intentional public contracts for the Phase 1 HTTP API."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"


class TaskListItem(BaseModel):
    id: str
    title: str
    difficulty: str
    status: str
    model_tier: str | None
    writer: str | None


class VerificationResultResponse(BaseModel):
    status: str
    sequence_id: int
    timestamp: datetime
    exit_code: int | None = None
    duration_seconds: float | None = None
    timed_out: bool | None = None
    stdout: str | None = None
    stderr: str | None = None
    error: str | None = None


class TaskDetailResponse(TaskListItem):
    description: str
    acceptance_criteria: list[str]
    model_name: str | None
    workspace_id: str | None
    current_attempt: int
    verification_status: str
    verification_result: VerificationResultResponse | None
    created_at: datetime
    updated_at: datetime


class EventResponse(BaseModel):
    sequence_id: int
    timestamp: datetime
    event_type: str
    actor_type: str
    actor_id: str
    execution_id: str | None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DiffResponse(BaseModel):
    task_id: str
    diff: str
