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
    ArtifactResponse,
    ChecklistEvaluationResponse,
    CreateArtifactRequest,
    CreateChecklistRequest,
    CreateTaskRequest,
    CreateTaskResponse,
    DiffResponse,
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
)
from ai_platform.api.security import DeveloperDependency, UserDependency
from ai_platform.model_preferences import ModelPreferenceService
from ai_platform.models import LogicalModel
from ai_platform.router import ModelRoutingError
from ai_platform.task_creation import CreateTaskCommand
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
        workflow_phase=record.workflow_phase.value,
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
    return presenter.task_detail(session, user, queued, actions, latest_readiness=latest_readiness)


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
