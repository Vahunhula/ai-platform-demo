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
    MODEL_SELECTED = "MODEL_SELECTED"
    STATUS_CHANGED = "STATUS_CHANGED"
    HUMAN_MESSAGE = "HUMAN_MESSAGE"
    AGENT_MESSAGE = "AGENT_MESSAGE"
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
    task_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: EventType
    actor_type: ActorType
    actor_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
