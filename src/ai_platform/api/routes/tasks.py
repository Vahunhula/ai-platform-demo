"""Read-only task, history, and workspace-diff endpoints."""

from fastapi import APIRouter

from ai_platform.api.dependencies import (
    ContextDependency,
    ControlsDependency,
    PresenterDependency,
    TaskCreationDependency,
)
from ai_platform.api.presenters import queued_count
from ai_platform.api.schemas import (
    CreateTaskRequest,
    CreateTaskResponse,
    DiffResponse,
    EventResponse,
    TaskDetailResponse,
    TaskListItem,
)
from ai_platform.api.security import DeveloperDependency, UserDependency
from ai_platform.task_creation import CreateTaskCommand

router = APIRouter(prefix="/tasks", tags=["tasks"])

# Handlers are plain ``def``: FastAPI runs them in its threadpool, so blocking
# SQLite/Git reads never stall the event loop that serves SSE streams.


@router.post("", response_model=CreateTaskResponse, status_code=201)
def create_task(
    body: CreateTaskRequest,
    creation: TaskCreationDependency,
    user: DeveloperDependency,
) -> CreateTaskResponse:
    record = creation.create(CreateTaskCommand(**body.model_dump()), user)
    return CreateTaskResponse(
        id=record.task_id,
        title=record.title,
        description=record.description or "",
        repository_id=record.repository_id or "",
        base_branch=record.base_branch or "",
        assignee_user_id=record.assignee_user_id or "",
        jira_key=record.jira_key,
        status="READY",
        created_by=record.created_by or user.username,
        created_at=record.created_at,
    )


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
    controls: ControlsDependency,
    presenter: PresenterDependency,
    user: UserDependency,
) -> TaskDetailResponse:
    session = context.sessions.get_session(task_id)
    queued = queued_count(context.storage.list_queued_messages(session.definition.id))
    actions = controls.availability(session.record, user)
    return presenter.task_detail(session, user, queued, actions)


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
