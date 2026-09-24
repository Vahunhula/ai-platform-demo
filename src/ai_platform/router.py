"""Authoritative logical-model catalog and deterministic task/phase routing."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ai_platform.config import Settings
from ai_platform.models import (
    LogicalModel,
    ModelResolutionSource,
    ModelSelection,
    ModelTier,
    TaskDifficulty,
)
from ai_platform.workflow import WorkflowPhase

if TYPE_CHECKING:
    from ai_platform.storage import SQLiteStorage


class ModelRoutingError(RuntimeError):
    """A safe routing/configuration failure suitable for a client."""


class ModelUnavailableError(ModelRoutingError):
    """A selected logical model has no usable configured provider target."""


class ModelCatalogEntry(BaseModel):
    logical_id: LogicalModel
    display_name: str
    provider: str | None
    configured_model_id: str | None
    enabled: bool


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    """Central logical-alias to provider-target configuration boundary."""

    entries: dict[LogicalModel, ModelCatalogEntry]

    @classmethod
    def from_settings(cls, settings: Settings) -> "ModelCatalog":
        sonnet = settings.default_model.strip()
        opus = settings.strong_model.strip()
        return cls(
            {
                LogicalModel.AUTO: ModelCatalogEntry(
                    logical_id=LogicalModel.AUTO,
                    display_name="Auto",
                    provider=None,
                    configured_model_id=None,
                    enabled=True,
                ),
                LogicalModel.CLAUDE_SONNET: ModelCatalogEntry(
                    logical_id=LogicalModel.CLAUDE_SONNET,
                    display_name="Claude Sonnet",
                    provider="anthropic",
                    configured_model_id=sonnet or None,
                    enabled=bool(sonnet),
                ),
                LogicalModel.CLAUDE_OPUS: ModelCatalogEntry(
                    logical_id=LogicalModel.CLAUDE_OPUS,
                    display_name="Claude Opus",
                    provider="anthropic",
                    configured_model_id=opus or None,
                    enabled=bool(opus),
                ),
            }
        )

    def entry(self, logical_id: LogicalModel) -> ModelCatalogEntry:
        return self.entries[logical_id]

    def concrete(self, logical_id: LogicalModel) -> ModelCatalogEntry:
        entry = self.entry(logical_id)
        if logical_id is LogicalModel.AUTO:
            raise ModelRoutingError("AUTO must resolve through AutoModelPolicy")
        if not entry.enabled or not entry.provider or not entry.configured_model_id:
            raise ModelUnavailableError(
                f"{entry.display_name} is unavailable because its model ID is not configured"
            )
        return entry

    def public_entries(self) -> list[ModelCatalogEntry]:
        """Return stable catalog order; API presenters omit configured model IDs."""

        return [self.entries[logical_id] for logical_id in LogicalModel]


@dataclass(frozen=True, slots=True)
class AutoModelPolicy:
    """Simple platform-owned phase policy; no model-generated confidence input."""

    def select(self, phase: WorkflowPhase) -> LogicalModel:
        policy = {
            WorkflowPhase.BRAINSTORM: LogicalModel.CLAUDE_SONNET,
            WorkflowPhase.PLAN: LogicalModel.CLAUDE_SONNET,
            WorkflowPhase.IMPLEMENTATION: LogicalModel.CLAUDE_SONNET,
            WorkflowPhase.REVIEW: LogicalModel.CLAUDE_OPUS,
        }
        try:
            return policy[phase]
        except KeyError as error:
            raise ModelRoutingError("HUMAN_REVIEW does not use an agent model") from error


@dataclass(frozen=True, slots=True)
class ModelRouter:
    """Resolve preferences and retain the legacy bounded escalation surface."""

    model_by_tier: dict[ModelTier, str]
    catalog: ModelCatalog | None = None
    storage: "SQLiteStorage | None" = None
    auto_policy: AutoModelPolicy = AutoModelPolicy()

    @classmethod
    def from_settings(
        cls, settings: Settings, storage: "SQLiteStorage | None" = None
    ) -> "ModelRouter":
        return cls(
            model_by_tier={
                ModelTier.CHEAP: settings.cheap_model,
                ModelTier.DEFAULT: settings.default_model,
                ModelTier.STRONG: settings.strong_model,
            },
            catalog=ModelCatalog.from_settings(settings),
            storage=storage,
        )

    def resolve(
        self,
        task_id: str,
        phase: WorkflowPhase,
        *,
        storage: "SQLiteStorage | None" = None,
    ) -> ModelSelection:
        """Resolve phase override → task default → AUTO policy → catalog target."""

        store = storage or self.storage
        if store is None:
            raise ModelRoutingError("ModelRouter is not bound to task storage")
        catalog = self.catalog or self._compatibility_catalog()
        if phase is WorkflowPhase.HUMAN_REVIEW:
            raise ModelRoutingError("HUMAN_REVIEW does not use an agent model")
        task = store.get_task(task_id)
        if task is None:
            raise ModelRoutingError(f"Unknown task: {task_id}")
        preference = store.get_phase_model_preference(task_id, phase)
        phase_selection = preference.model_selection if preference else None
        if phase_selection not in {None, LogicalModel.AUTO}:
            requested = phase_selection
            effective = phase_selection
            source = ModelResolutionSource.PHASE_OVERRIDE
        elif task.default_model_selection is not LogicalModel.AUTO:
            requested = task.default_model_selection
            effective = task.default_model_selection
            source = ModelResolutionSource.TASK_DEFAULT
        else:
            requested = LogicalModel.AUTO
            effective = self.auto_policy.select(phase)
            source = ModelResolutionSource.AUTO_POLICY
        entry = catalog.concrete(effective)
        tier = ModelTier.DEFAULT if effective is LogicalModel.CLAUDE_SONNET else ModelTier.STRONG
        return ModelSelection(
            tier=tier,
            model=entry.configured_model_id,
            reason=f"{source.value}: {requested.value} resolved to {effective.value}",
            requested_selection=requested,
            effective_selection=effective,
            provider=entry.provider,
            resolution_source=source,
            workflow_phase=phase,
        )

    def escalate_selection(
        self, selection: ModelSelection, failed_attempt_count: int
    ) -> ModelSelection | None:
        """Escalate only AUTO-policy Sonnet; concrete user intent never silently changes."""

        if (
            selection.resolution_source is not ModelResolutionSource.AUTO_POLICY
            or selection.effective_selection is not LogicalModel.CLAUDE_SONNET
        ):
            return None
        entry = (self.catalog or self._compatibility_catalog()).concrete(LogicalModel.CLAUDE_OPUS)
        return ModelSelection(
            tier=ModelTier.STRONG,
            model=entry.configured_model_id,
            reason=f"AUTO escalation after {failed_attempt_count} failed Sonnet attempts",
            requested_selection=LogicalModel.AUTO,
            effective_selection=LogicalModel.CLAUDE_OPUS,
            provider=entry.provider,
            resolution_source=ModelResolutionSource.AUTO_POLICY,
            workflow_phase=selection.workflow_phase,
        )

    def _compatibility_catalog(self) -> ModelCatalog:
        """Support legacy direct construction while application wiring uses Settings."""

        return ModelCatalog(
            {
                LogicalModel.AUTO: ModelCatalogEntry(
                    logical_id=LogicalModel.AUTO,
                    display_name="Auto",
                    provider=None,
                    configured_model_id=None,
                    enabled=True,
                ),
                LogicalModel.CLAUDE_SONNET: ModelCatalogEntry(
                    logical_id=LogicalModel.CLAUDE_SONNET,
                    display_name="Claude Sonnet",
                    provider="anthropic",
                    configured_model_id=self.model_by_tier[ModelTier.DEFAULT],
                    enabled=bool(self.model_by_tier[ModelTier.DEFAULT]),
                ),
                LogicalModel.CLAUDE_OPUS: ModelCatalogEntry(
                    logical_id=LogicalModel.CLAUDE_OPUS,
                    display_name="Claude Opus",
                    provider="anthropic",
                    configured_model_id=self.model_by_tier[ModelTier.STRONG],
                    enabled=bool(self.model_by_tier[ModelTier.STRONG]),
                ),
            }
        )

    # Compatibility helpers retained for existing deterministic router callers.
    def select(self, difficulty: TaskDifficulty) -> ModelSelection:
        tier_by_difficulty = {
            TaskDifficulty.LOW: ModelTier.CHEAP,
            TaskDifficulty.MEDIUM: ModelTier.DEFAULT,
            TaskDifficulty.HIGH: ModelTier.STRONG,
        }
        tier = tier_by_difficulty[difficulty]
        return ModelSelection(
            tier=tier,
            model=self.model_by_tier[tier],
            reason=f"Initial routing based on task difficulty: {difficulty.value}",
        )

    def select_tier(self, tier: ModelTier, reason: str) -> ModelSelection:
        return ModelSelection(tier=tier, model=self.model_by_tier[tier], reason=reason)

    @staticmethod
    def next_tier(tier: ModelTier) -> ModelTier | None:
        return {
            ModelTier.CHEAP: ModelTier.DEFAULT,
            ModelTier.DEFAULT: ModelTier.STRONG,
            ModelTier.STRONG: None,
        }[tier]

    def escalate(self, tier: ModelTier, failed_attempt_count: int) -> ModelSelection | None:
        next_tier = self.next_tier(tier)
        if next_tier is None:
            return None
        return self.select_tier(
            next_tier,
            reason=f"Verification failed {failed_attempt_count} times at {tier.value} tier.",
        )
