"""Authenticated application service for durable model-routing preferences."""

from ai_platform.auth import AuthenticatedUser
from ai_platform.models import LogicalModel
from ai_platform.router import ModelCatalog, ModelRoutingError
from ai_platform.storage import SQLiteStorage
from ai_platform.workflow import WorkflowPhase


class ModelPreferenceService:
    def __init__(self, storage: SQLiteStorage, catalog: ModelCatalog) -> None:
        self.storage = storage
        self.catalog = catalog

    def set_default(self, task_id: str, selection: LogicalModel, actor: AuthenticatedUser) -> bool:
        self._authorize(actor)
        self._validate_available(selection)
        try:
            return self.storage.set_default_model_selection(
                task_id, selection, actor.username, actor.display_name
            )
        except KeyError as error:
            raise ModelRoutingError(f"Unknown task: {task_id}") from error

    def set_phase(
        self,
        task_id: str,
        phase: WorkflowPhase,
        selection: LogicalModel,
        actor: AuthenticatedUser,
    ) -> bool:
        self._authorize(actor)
        if phase is WorkflowPhase.HUMAN_REVIEW:
            raise ModelRoutingError("HUMAN_REVIEW does not use an agent model")
        self._validate_available(selection)
        try:
            return self.storage.set_phase_model_preference(
                task_id, phase, selection, actor.username, actor.display_name
            )
        except KeyError as error:
            raise ModelRoutingError(f"Unknown task: {task_id}") from error

    def _validate_available(self, selection: LogicalModel) -> None:
        if selection is not LogicalModel.AUTO:
            self.catalog.concrete(selection)

    @staticmethod
    def _authorize(actor: AuthenticatedUser) -> None:
        if not actor.role.can_modify_tasks:
            raise ModelRoutingError("Read-only access: your role cannot change model routing")
