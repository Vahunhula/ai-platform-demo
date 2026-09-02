"""Provider-independent model routing policy."""

from dataclasses import dataclass

from ai_platform.config import Settings
from ai_platform.models import ModelSelection, ModelTier, TaskDifficulty


@dataclass(frozen=True, slots=True)
class ModelRouter:
    """Select an initial tier and progress upward after verification evidence."""

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
        """Return the initial selection based only on declared difficulty."""

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
        """Select a configured provider model for an explicit current tier."""

        return ModelSelection(tier=tier, model=self.model_by_tier[tier], reason=reason)

    @staticmethod
    def next_tier(tier: ModelTier) -> ModelTier | None:
        """Return the one-way escalation target, never a downgrade."""

        progression = {
            ModelTier.CHEAP: ModelTier.DEFAULT,
            ModelTier.DEFAULT: ModelTier.STRONG,
            ModelTier.STRONG: None,
        }
        return progression[tier]

    def escalate(self, tier: ModelTier, failed_attempt_count: int) -> ModelSelection | None:
        """Return the next configured tier after repeated deterministic failures."""

        next_tier = self.next_tier(tier)
        if next_tier is None:
            return None
        return self.select_tier(
            next_tier,
            reason=(
                f"Verification failed {failed_attempt_count} times at {tier.value} tier."
            ),
        )
