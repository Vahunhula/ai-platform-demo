"""Read-only task, history, and workspace-diff endpoints."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response

from ai_platform.api.dependencies import (
    ContextDependency,
    ControlsDependency,
    PresenterDependency,
    RemovalDependency,
    TaskCreationDependency,
)
from ai_platform.api.presenters import queued_count
from ai_platform.api.schemas import (
    ArtifactResponse,
    ChecklistEvaluationResponse,
    CreateArtifactRequest,
    CreateChecklistRequest,
    CreateTaskRequest,
    CreateTaskResponse,
    DiffResponse,
    EventPageResponse,
    EventResponse,
    ModelPreferenceRequest,
    ModelPreferenceUpdateResponse,
    PhaseModelRoutingResponse,
    PhaseTransitionRequest,
    PhaseTransitionResponse,
    ReadinessSummary,
    ResolvedModelResponse,
    TaskDetailResponse,
    TaskListItem,
    TaskModelRoutingResponse,
    TaskTestFileResponse,
    TaskTestsResponse,
)
from ai_platform.api.security import DeveloperDependency, UserDependency
from ai_platform.model_preferences import ModelPreferenceService
from ai_platform.models import LogicalModel, TestFileSource
from ai_platform.router import ModelRoutingError
from ai_platform.task_creation import CreateTaskCommand
from ai_platform.task_tests import UploadedTestInput
from ai_platform.workflow import TransitionMode, WorkflowPhase
from ai_platform.workflow_services import (
    ChecklistService,
    WorkflowArtifactService,
    WorkflowPhaseService,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])

# Handlers are plain ``def``: FastAPI runs them in its threadpool, so blocking
# SQLite/Git reads never stall the event loop that serves SSE streams.


@router.post("", response_model=CreateTaskResponse, status_code=201)
def create_task(
    body: CreateTaskRequest,
    creation: TaskCreationDependency,
    user: DeveloperDependency,
) -> CreateTaskResponse:
    values = body.model_dump(exclude={"uploaded_test_files"})
    uploads = tuple(
        UploadedTestInput(filename=item.filename, content=item.content)
        for item in body.uploaded_test_files
    )
    record = creation.create(CreateTaskCommand(**values, uploaded_test_files=uploads), user)
    return CreateTaskResponse(
        id=record.task_id,
        title=record.title,
        description=record.description or "",
        repository_id=record.repository_id or "",
        base_branch=record.base_branch or "",
        assignee_user_id=record.assignee_user_id or "",
        jira_key=record.jira_key,
        status="READY",
        workflow_phase=record.workflow_phase.value,
        created_by=record.created_by or user.username,
        created_at=record.created_at,
    )


@router.get("/{task_id}/tests", response_model=TaskTestsResponse)
def get_task_tests(
    task_id: str,
    context: ContextDependency,
    presenter: PresenterDependency,
) -> TaskTestsResponse:
    canonical_id = context.sessions.get_definition(task_id).id
    specification = context.storage.get_test_specification(canonical_id)
    files = context.storage.list_task_test_files(canonical_id)
    workspace = context.sessions.resolve_workspace(canonical_id, require_exists=True)
    repository_tests = (
        sorted(
            path.relative_to(workspace).as_posix()
            for path in (workspace / "tests").rglob("*")
            if path.is_file()
        )
        if (workspace / "tests").is_dir()
        else []
    )

    def response(item):
        return TaskTestFileResponse(**item.model_dump(exclude={"file_id", "task_id"}))

    return TaskTestsResponse(
        task_id=canonical_id,
        human_requirements=specification.original_text if specification else None,
        requirements_created_by=specification.created_by if specification else None,
        generation_status=(
            specification.generation_status.value if specification else "NOT_REQUESTED"
        ),
        generation_message=specification.generation_message if specification else None,
        generated_tests=[
            response(item) for item in files if item.source is TestFileSource.GENERATED
        ],
        uploaded_tests=[response(item) for item in files if item.source is TestFileSource.UPLOADED],
        repository_tests=repository_tests,
        latest_verification=presenter.verification_result(context.storage.get_events(canonical_id)),
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
    request: Request,
    context: ContextDependency,
    controls: ControlsDependency,
    presenter: PresenterDependency,
    user: UserDependency,
    removal: RemovalDependency,
) -> TaskDetailResponse:
    session = context.sessions.get_session(task_id)
    queued = queued_count(context.storage.list_queued_messages(session.definition.id))
    actions = controls.availability(session.record, user)
    evaluations = context.storage.list_checklist_evaluations(session.definition.id)
    latest_readiness = (
        ReadinessSummary(
            phase=evaluations[-1].phase,
            score=evaluations[-1].readiness.score,
            eligible_for_auto_progression=evaluations[-1].readiness.eligible_for_auto_progression,
        )
        if evaluations
        else None
    )
    removal_state = removal.availability(session.record, user)
    assignees = (
        request.app.state.auth.users_by_id([session.record.assignee_user_id])
        if session.record.assignee_user_id
        else {}
    )
    assignee = assignees.get(session.record.assignee_user_id or "")
    return presenter.task_detail(
        session,
        user,
        queued,
        actions,
        latest_readiness=latest_readiness,
        removal=removal_state,
        assignee_display_name=assignee.display_name if assignee else None,
    )


@router.delete(
    "/{task_id}",
    status_code=204,
    responses={401: {}, 403: {}, 404: {}, 409: {}},
)
def remove_task(
    task_id: str,
    removal: RemovalDependency,
    user: DeveloperDependency,
) -> Response:
    """Permanently remove one terminal TaskSession and its owned resources."""

    removal.remove(task_id.upper(), user)
    return Response(status_code=204)


@router.get("/{task_id}/events", response_model=list[EventResponse])
def get_events(
    task_id: str,
    context: ContextDependency,
    presenter: PresenterDependency,
    order: Literal["asc", "desc"] = "asc",
) -> list[EventResponse]:
    """Return the durable event log. ``order`` is presentation-only.

    Canonical storage and SSE delivery are always chronological; ``desc``
    only reverses the list this endpoint returns, for an Activity view that
    defaults to newest-first without touching sequence-number semantics.
    """

    events = [presenter.event(event) for event in context.sessions.get_events(task_id)]
    return list(reversed(events)) if order == "desc" else events


@router.get("/{task_id}/events/page", response_model=EventPageResponse)
def get_event_page(
    task_id: str,
    context: ContextDependency,
    presenter: PresenterDependency,
    order: Literal["asc", "desc"] = "desc",
    limit: int = Query(default=50, ge=1, le=200),
    before_sequence: int | None = Query(default=None, ge=1),
    after_sequence: int | None = Query(default=None, ge=0),
) -> EventPageResponse:
    """Return a cursor-stable bounded page for Activity.

    The original ``/events`` list contract remains unchanged for CLI and older
    consumers. Descending traversal uses ``before_sequence``; ascending traversal
    uses ``after_sequence``. Both cursors are exclusive.
    """

    canonical_id = context.sessions.get_definition(task_id).id
    if order == "desc" and after_sequence is not None:
        raise HTTPException(422, "after_sequence is only valid with order=asc")
    if order == "asc" and before_sequence is not None:
        raise HTTPException(422, "before_sequence is only valid with order=desc")
    cursor = before_sequence if order == "desc" else after_sequence
    events, has_more = context.storage.get_event_page(
        canonical_id, order=order, limit=limit, cursor=cursor
    )
    items = [presenter.event(event) for event in events]
    return EventPageResponse(
        items=items,
        order=order,
        limit=limit,
        has_more=has_more,
        next_before_sequence=(items[-1].sequence_id if order == "desc" and has_more else None),
        next_after_sequence=(items[-1].sequence_id if order == "asc" and has_more else None),
    )


@router.get("/{task_id}/diff", response_model=DiffResponse)
def get_diff(task_id: str, context: ContextDependency) -> DiffResponse:
    definition = context.sessions.get_definition(task_id)
    return DiffResponse(task_id=definition.id, diff=context.sessions.get_diff(task_id))


_MODEL_PHASES = (
    WorkflowPhase.BRAINSTORM,
    WorkflowPhase.PLAN,
    WorkflowPhase.IMPLEMENTATION,
    WorkflowPhase.REVIEW,
)


@router.get("/{task_id}/model-routing", response_model=TaskModelRoutingResponse)
def get_model_routing(task_id: str, context: ContextDependency) -> TaskModelRoutingResponse:
    task_id = context.sessions.get_definition(task_id).id
    record = context.storage.get_task(task_id)
    preferences = {
        item.phase: item.model_selection
        for item in context.storage.list_phase_model_preferences(task_id)
    }
    phases: list[PhaseModelRoutingResponse] = []
    for phase in _MODEL_PHASES:
        selection = preferences.get(phase, LogicalModel.AUTO)
        try:
            resolved = context.model_router.resolve(task_id, phase)
            result = ResolvedModelResponse(
                requested_selection=resolved.requested_selection,
                effective_selection=resolved.effective_selection,
                provider=resolved.provider,
                concrete_model_id=resolved.model,
                source=resolved.resolution_source,
            )
            phases.append(
                PhaseModelRoutingResponse(phase=phase, selection=selection, resolved=result)
            )
        except ModelRoutingError as error:
            phases.append(
                PhaseModelRoutingResponse(phase=phase, selection=selection, error=str(error))
            )
    return TaskModelRoutingResponse(
        task_id=task_id,
        default_model_selection=record.default_model_selection,
        phases=phases,
    )


@router.put("/{task_id}/model-routing/default", response_model=ModelPreferenceUpdateResponse)
def set_default_model(
    task_id: str,
    body: ModelPreferenceRequest,
    context: ContextDependency,
    user: DeveloperDependency,
) -> ModelPreferenceUpdateResponse:
    task_id = context.sessions.get_definition(task_id).id
    changed = ModelPreferenceService(context.storage, context.model_catalog).set_default(
        task_id, body.selection, user
    )
    return ModelPreferenceUpdateResponse(task_id=task_id, selection=body.selection, changed=changed)


@router.put(
    "/{task_id}/model-routing/phases/{phase}",
    response_model=ModelPreferenceUpdateResponse,
)
def set_phase_model(
    task_id: str,
    phase: WorkflowPhase,
    body: ModelPreferenceRequest,
    context: ContextDependency,
    user: DeveloperDependency,
) -> ModelPreferenceUpdateResponse:
    task_id = context.sessions.get_definition(task_id).id
    changed = ModelPreferenceService(context.storage, context.model_catalog).set_phase(
        task_id, phase, body.selection, user
    )
    return ModelPreferenceUpdateResponse(
        task_id=task_id, phase=phase, selection=body.selection, changed=changed
    )


@router.post("/{task_id}/phase", response_model=PhaseTransitionResponse)
def transition_phase(
    task_id: str,
    body: PhaseTransitionRequest,
    context: ContextDependency,
    user: DeveloperDependency,
) -> PhaseTransitionResponse:
    task_id = context.sessions.get_definition(task_id).id
    phase = WorkflowPhaseService(context.storage).transition(
        task_id,
        body.from_phase,
        body.to_phase,
        user,
        reason=body.reason,
        mode=TransitionMode.MANUAL,
    )
    return PhaseTransitionResponse(
        task_id=task_id,
        from_phase=body.from_phase,
        workflow_phase=phase,
    )


@router.get("/{task_id}/artifacts", response_model=list[ArtifactResponse])
def list_artifacts(
    task_id: str,
    context: ContextDependency,
    presenter: PresenterDependency,
    current: bool = False,
) -> list[ArtifactResponse]:
    task_id = context.sessions.get_definition(task_id).id
    return [
        ArtifactResponse(
            **{
                **artifact.model_dump(),
                "payload": presenter.redactor.value(artifact.payload),
            }
        )
        for artifact in context.storage.list_workflow_artifacts(task_id, current_only=current)
    ]


@router.post("/{task_id}/artifacts", response_model=ArtifactResponse, status_code=201)
def create_artifact(
    task_id: str,
    body: CreateArtifactRequest,
    context: ContextDependency,
    presenter: PresenterDependency,
    user: DeveloperDependency,
) -> ArtifactResponse:
    task_id = context.sessions.get_definition(task_id).id
    artifact = WorkflowArtifactService(context.storage).create(
        task_id, body.phase, body.kind, body.payload, user
    )
    return ArtifactResponse(
        **{
            **artifact.model_dump(),
            "payload": presenter.redactor.value(artifact.payload),
        }
    )


@router.get("/{task_id}/checklists", response_model=list[ChecklistEvaluationResponse])
def list_checklists(
    task_id: str, context: ContextDependency, presenter: PresenterDependency
) -> list[ChecklistEvaluationResponse]:
    task_id = context.sessions.get_definition(task_id).id
    return [
        _checklist_response(evaluation.model_dump(), presenter)
        for evaluation in context.storage.list_checklist_evaluations(task_id)
    ]


@router.post(
    "/{task_id}/checklists",
    response_model=ChecklistEvaluationResponse,
    status_code=201,
)
def create_checklist(
    task_id: str,
    body: CreateChecklistRequest,
    context: ContextDependency,
    presenter: PresenterDependency,
    user: DeveloperDependency,
) -> ChecklistEvaluationResponse:
    task_id = context.sessions.get_definition(task_id).id
    evaluation = ChecklistService(context.storage).evaluate(task_id, body.phase, body.items, user)
    return _checklist_response(evaluation.model_dump(), presenter)


def _checklist_response(
    data: dict[str, object], presenter: PresenterDependency
) -> ChecklistEvaluationResponse:
    """Redact configured server paths from exposed checklist evidence."""

    return ChecklistEvaluationResponse(**presenter.redactor.value(data))
