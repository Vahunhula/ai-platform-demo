"""Provider-independent model routing policy."""

from dataclasses import dataclass

from ai_platform.config import Settings
from ai_platform.models import ModelSelection, ModelTier, TaskDifficulty


@dataclass(frozen=True, slots=True)
class ModelRouter:
    """Route a task to its initial model tier based on declared difficulty."""

    model_by_tier: dict[ModelTier, str]

    @classmethod
    def from_settings(cls, settings: Settings) -> "ModelRouter":
        """Build a router from centrally configured provider model IDs."""

        return cls(
            model_by_tier={
                ModelTier.CHEAP: settings.cheap_model,
                ModelTier.DEFAULT: settings.default_model,
                ModelTier.STRONG: settings.strong_model,
            }
        )

    def select(self, difficulty: TaskDifficulty) -> ModelSelection:
        """Return the initial selection; later policies can add escalation."""

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
