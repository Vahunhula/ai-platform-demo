"""Read-only task, history, and workspace-diff endpoints."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from ai_platform.api.dependencies import get_context
from ai_platform.api.schemas import (
    DiffResponse,
    EventResponse,
    TaskDetailResponse,
    TaskListItem,
    VerificationResultResponse,
)
from ai_platform.application import ApplicationContext
from ai_platform.events import Event, EventType
from ai_platform.models import TaskRecord
from ai_platform.sessions import TaskSession

router = APIRouter(prefix="/tasks", tags=["tasks"])
ContextDependency = Annotated[ApplicationContext, Depends(get_context)]

_COMMON_PUBLIC_METADATA = {
    "attempt",
    "turn_attempt",
    "tier_attempt",
    "continuation",
    "difficulty",
    "tier",
    "model",
    "reason",
    "previous_tier",
    "previous_model",
    "new_tier",
    "new_model",
    "failed_attempt_count",
    "display_name",
    "from",
    "to",
    "summary",
    "message",
    "error",
    "fatal",
    "cancelled",
    "change_type",
    "files",
    "deferred",
    "active_execution",
    "exit_code",
    "duration_seconds",
    "timed_out",
    "started_at",
    "finished_at",
    "stdout",
    "stderr",
    "workspace_retained",
    "sdk_version",
    "authentication_method",
    "activity",
    "tool",
    "is_error",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "turns",
    "duration_ms",
    "duration_api_ms",
    "total_cost_usd",
    "models",
}


def _writer(record: TaskRecord) -> str | None:
    if record.active_execution is None:
        return None
    return record.execution_actor_id or record.active_execution.value


def _verification_result(events: list[Event]) -> VerificationResultResponse | None:
    for event in reversed(events):
        if event.event_type not in {EventType.TEST_PASSED, EventType.TEST_FAILED}:
            continue
        metadata = event.metadata
        return VerificationResultResponse(
            status="passed" if event.event_type is EventType.TEST_PASSED else "failed",
            sequence_id=event.sequence_id or 0,
            timestamp=event.timestamp,
            exit_code=metadata.get("exit_code"),
            duration_seconds=metadata.get("duration_seconds"),
            timed_out=metadata.get("timed_out"),
            stdout=metadata.get("stdout"),
            stderr=metadata.get("stderr"),
            error=metadata.get("error"),
        )
    return None


def _task_item(context: ApplicationContext, record: TaskRecord) -> TaskListItem:
    definition = context.sessions.get_definition(record.task_id)
    return TaskListItem(
        id=definition.id,
        title=definition.title,
        difficulty=definition.difficulty.value.upper(),
        status=record.status.value.upper(),
        model_tier=(record.selected_tier.value if record.selected_tier else None),
        writer=_writer(record),
    )


def _task_detail(context: ApplicationContext, session: TaskSession) -> TaskDetailResponse:
    record = session.record
    return TaskDetailResponse(
        **_task_item(context, record).model_dump(),
        description=session.definition.description,
        acceptance_criteria=session.definition.acceptance_criteria,
        model_name=record.selected_model,
        workspace_id=session.definition.id if record.workspace_path else None,
        current_attempt=record.attempt,
        verification_status=record.verification_status.value.upper(),
        verification_result=_verification_result(session.events),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _public_metadata(event: Event) -> dict[str, Any]:
    metadata = {
        key: value for key, value in event.metadata.items() if key in _COMMON_PUBLIC_METADATA
    }
    if event.event_type is EventType.FILE_CHANGED and "path" in event.metadata:
        metadata["path"] = event.metadata["path"]
    if (
        event.event_type in {EventType.TEST_STARTED, EventType.TEST_PASSED, EventType.TEST_FAILED}
        and "command" in event.metadata
    ):
        metadata["command"] = event.metadata["command"]
    return metadata


@router.get("", response_model=list[TaskListItem])
async def list_tasks(context: ContextDependency) -> list[TaskListItem]:
    return [_task_item(context, record) for record in context.sessions.list_tasks()]


@router.get("/{task_id}", response_model=TaskDetailResponse)
async def get_task(
    task_id: str,
    context: ContextDependency,
) -> TaskDetailResponse:
    return _task_detail(context, context.sessions.get_session(task_id))


@router.get("/{task_id}/events", response_model=list[EventResponse])
async def get_events(
    task_id: str,
    context: ContextDependency,
) -> list[EventResponse]:
    return [
        EventResponse(
            sequence_id=event.sequence_id or 0,
            timestamp=event.timestamp,
            event_type=event.event_type.value,
            actor_type=event.actor_type.value,
            actor_id=event.actor_id,
            execution_id=event.metadata.get("execution_id"),
            metadata=_public_metadata(event),
        )
        for event in context.sessions.get_events(task_id)
    ]


@router.get("/{task_id}/diff", response_model=DiffResponse)
async def get_diff(
    task_id: str,
    context: ContextDependency,
) -> DiffResponse:
    definition = context.sessions.get_definition(task_id)
    return DiffResponse(task_id=definition.id, diff=context.sessions.get_diff(task_id))
