"""Application services for explicit workflow mutations."""

from ai_platform.auth import AuthenticatedUser
from ai_platform.events import ActorType, Event, EventType
from ai_platform.storage import SQLiteStorage
from ai_platform.workflow import (
    ARTIFACT_PHASES,
    ArtifactKind,
    ChecklistEvaluation,
    ChecklistResult,
    TransitionMode,
    WorkflowArtifact,
    WorkflowPhase,
    calculate_readiness,
    resolve_checklist,
    validate_artifact_payload,
)


class WorkflowError(RuntimeError):
    """Client-safe workflow domain failure."""


class WorkflowConflictError(WorkflowError):
    """The expected phase lost a concurrent transition race."""


class WorkflowPhaseService:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def transition(
        self,
        task_id: str,
        expected_from: WorkflowPhase,
        target: WorkflowPhase,
        actor: AuthenticatedUser,
        *,
        reason: str | None = None,
        mode: TransitionMode = TransitionMode.MANUAL,
    ) -> WorkflowPhase:
        if not actor.role.can_modify_tasks:
            raise WorkflowError("Read-only access: your role cannot change workflow phase")
        if mode is not TransitionMode.MANUAL:
            raise WorkflowError("Automatic workflow transitions are not enabled in Phase 2")
        if expected_from is target:
            raise WorkflowError("Workflow phase must change")
        normalized_reason = reason.strip() if reason else None
        if normalized_reason and len(normalized_reason) > 4000:
            raise WorkflowError("Transition reason is too long")
        event = Event(
            task_id=task_id,
            event_type=EventType.WORKFLOW_PHASE_CHANGED,
            actor_type=ActorType.HUMAN,
            actor_id=actor.username,
            metadata={
                "from_phase": expected_from.value,
                "to_phase": target.value,
                "transition_mode": mode.value,
                "display_name": actor.display_name,
                **({"reason": normalized_reason} if normalized_reason else {}),
            },
        )
        try:
            changed = self.storage.transition_workflow_phase(task_id, expected_from, target, event)
        except KeyError as error:
            raise WorkflowError(f"Unknown task: {task_id}") from error
        if not changed:
            current = self.storage.get_task(task_id)
            actual = current.workflow_phase.value if current else "missing"
            raise WorkflowConflictError(
                f"Expected {expected_from.value}, but current workflow phase is {actual}"
            )
        return target


class WorkflowArtifactService:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def create(
        self,
        task_id: str,
        phase: WorkflowPhase,
        kind: ArtifactKind,
        payload: object,
        actor: AuthenticatedUser,
    ) -> WorkflowArtifact:
        if not actor.role.can_modify_tasks:
            raise WorkflowError("Read-only access: your role cannot create artifacts")
        if ARTIFACT_PHASES[kind] is not phase:
            raise WorkflowError(f"{kind.value} belongs to {ARTIFACT_PHASES[kind].value}")
        try:
            normalized = validate_artifact_payload(kind, payload)
        except ValueError as error:
            raise WorkflowError(f"Invalid {kind.value} payload: {error}") from error
        try:
            return self.storage.create_workflow_artifact(
                task_id, phase, kind, normalized, actor.username
            )
        except KeyError as error:
            raise WorkflowError(f"Unknown task: {task_id}") from error


class ChecklistService:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def evaluate(
        self,
        task_id: str,
        phase: WorkflowPhase,
        results: list[ChecklistResult],
        actor: AuthenticatedUser,
    ) -> ChecklistEvaluation:
        if not actor.role.can_modify_tasks:
            raise WorkflowError("Read-only access: your role cannot create checklist snapshots")
        try:
            items = resolve_checklist(phase, results)
        except ValueError as error:
            raise WorkflowError(str(error)) from error
        readiness = calculate_readiness(items)
        try:
            return self.storage.create_checklist_evaluation(
                task_id, phase, items, readiness, actor.username
            )
        except KeyError as error:
            raise WorkflowError(f"Unknown task: {task_id}") from error
