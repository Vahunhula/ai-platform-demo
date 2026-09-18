"""Intentional public contracts for the HTTP API (mirrored in web/src/types/api.ts)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_platform.sessions import MAX_MESSAGE_LENGTH


class HealthResponse(BaseModel):
    status: str = "ok"


class ConfigResponse(BaseModel):
    """What this API process allows the browser to do."""

    messaging_enabled: bool
    web_actor: str | None
    runner_enabled: bool
    max_message_length: int


class TaskListItem(BaseModel):
    id: str
    title: str
    difficulty: str
    status: str
    model_tier: str | None
    writer: str | None
    updated_at: datetime


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


class MessagingState(BaseModel):
    """Whether the browser may send a message to this task now, and why not."""

    accepting: bool
    reason: str | None


class TaskDetailResponse(TaskListItem):
    description: str
    acceptance_criteria: list[str]
    model_name: str | None
    workspace_id: str | None
    current_attempt: int
    verification_status: str
    verification_result: VerificationResultResponse | None
    agent_working: bool
    queued_messages: int
    messaging: MessagingState
    created_at: datetime


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


MessageStatusName = Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED"]


class MessageResponse(BaseModel):
    """One public conversation message (HUMAN_MESSAGE or AGENT_MESSAGE event)."""

    id: str
    task_id: str
    role: Literal["human", "agent"]
    actor_id: str
    content: str
    timestamp: datetime
    sequence_id: int
    turn_id: str | None
    # Delivery state; only browser-submitted human messages have one.
    status: MessageStatusName | None
    error: str | None
    client_message_id: str | None
    channel: str | None


class PostMessageRequest(BaseModel):
    """Browser message submission. The actor is resolved server-side, never sent."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(max_length=MAX_MESSAGE_LENGTH)
    client_message_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")

    @field_validator("message")
    @classmethod
    def message_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Message must not be empty")
        return stripped


class PostMessageResponse(BaseModel):
    status: Literal["accepted"] = "accepted"
    message_id: str
    client_message_id: str
    task_id: str
    message_status: MessageStatusName
    duplicate: bool
