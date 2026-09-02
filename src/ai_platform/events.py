"""Append-only audit event definitions."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class ActorType(StrEnum):
    """Kinds of actors that can create an audit event."""

    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class EventType(StrEnum):
    """Audit event types needed by the initial and planned workflows."""

    TASK_CREATED = "TASK_CREATED"
    TASK_STARTED = "TASK_STARTED"
    TASK_RESET = "TASK_RESET"
    WORKSPACE_CREATED = "WORKSPACE_CREATED"
    WORKSPACE_RESET = "WORKSPACE_RESET"
    MODEL_SELECTED = "MODEL_SELECTED"
    MODEL_ESCALATED = "MODEL_ESCALATED"
    STATUS_CHANGED = "STATUS_CHANGED"
    STALE_LOCK_RECOVERED = "STALE_LOCK_RECOVERED"
    HUMAN_CONNECTED = "HUMAN_CONNECTED"
    HUMAN_MESSAGE = "HUMAN_MESSAGE"
    HUMAN_PAUSED = "HUMAN_PAUSED"
    HUMAN_RESUMED = "HUMAN_RESUMED"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    HUMAN_REJECTED = "HUMAN_REJECTED"
    HUMAN_SHELL_OPENED = "HUMAN_SHELL_OPENED"
    HUMAN_SHELL_CLOSED = "HUMAN_SHELL_CLOSED"
    HUMAN_WORKSPACE_CHANGED = "HUMAN_WORKSPACE_CHANGED"
    AGENT_MESSAGE = "AGENT_MESSAGE"
    AGENT_STARTED = "AGENT_STARTED"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_FAILED = "AGENT_FAILED"
    AGENT_TOOL_ACTIVITY = "AGENT_TOOL_ACTIVITY"
    FILE_CHANGED = "FILE_CHANGED"
    TEST_STARTED = "TEST_STARTED"
    TEST_PASSED = "TEST_PASSED"
    TEST_FAILED = "TEST_FAILED"
    TASK_APPROVED = "TASK_APPROVED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"


class Event(BaseModel):
    """One immutable audit-history entry."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    sequence_id: int | None = None
    task_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: EventType
    actor_type: ActorType
    actor_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
