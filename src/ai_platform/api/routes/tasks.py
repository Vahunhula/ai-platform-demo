"""Read-only task, history, and workspace-diff endpoints."""

from fastapi import APIRouter

from ai_platform.api.dependencies import (
    ContextDependency,
    ControlsDependency,
    ConversationDependency,
    PresenterDependency,
)
from ai_platform.api.presenters import queued_count
from ai_platform.api.schemas import DiffResponse, EventResponse, TaskDetailResponse, TaskListItem

router = APIRouter(prefix="/tasks", tags=["tasks"])

# Handlers are plain ``def``: FastAPI runs them in its threadpool, so blocking
# SQLite/Git reads never stall the event loop that serves SSE streams.


@router.get("", response_model=list[TaskListItem])
def list_tasks(context: ContextDependency, presenter: PresenterDependency) -> list[TaskListItem]:
    return [
        presenter.task_item(context.sessions.get_definition(record.task_id), record)
        for record in context.sessions.list_tasks()
    ]


@router.get("/{task_id}", response_model=TaskDetailResponse)
def get_task(
    task_id: str,
    context: ContextDependency,
    conversation: ConversationDependency,
    controls: ControlsDependency,
    presenter: PresenterDependency,
) -> TaskDetailResponse:
    session = context.sessions.get_session(task_id)
    queued = queued_count(context.storage.list_queued_messages(session.definition.id))
    actions = controls.availability(session.record)
    return presenter.task_detail(session, conversation, queued, actions)


@router.get("/{task_id}/events", response_model=list[EventResponse])
def get_events(
    task_id: str,
    context: ContextDependency,
    presenter: PresenterDependency,
) -> list[EventResponse]:
    return [presenter.event(event) for event in context.sessions.get_events(task_id)]


@router.get("/{task_id}/diff", response_model=DiffResponse)
def get_diff(task_id: str, context: ContextDependency) -> DiffResponse:
    definition = context.sessions.get_definition(task_id)
    return DiffResponse(task_id=definition.id, diff=context.sessions.get_diff(task_id))
