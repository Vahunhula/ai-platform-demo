"""Tests for the initial task-difficulty routing policy."""

import pytest

from ai_platform.models import ModelTier, TaskDifficulty
from ai_platform.router import ModelRouter


@pytest.fixture
def router() -> ModelRouter:
    return ModelRouter(
        {
            ModelTier.CHEAP: "cheap-model",
            ModelTier.DEFAULT: "default-model",
            ModelTier.STRONG: "strong-model",
        }
    )


@pytest.mark.parametrize(
    ("difficulty", "expected_tier"),
    [
        (TaskDifficulty.LOW, ModelTier.CHEAP),
        (TaskDifficulty.MEDIUM, ModelTier.DEFAULT),
        (TaskDifficulty.HIGH, ModelTier.STRONG),
    ],
)
def test_routes_difficulty_to_expected_tier(
    router: ModelRouter,
    difficulty: TaskDifficulty,
    expected_tier: ModelTier,
) -> None:
    selection = router.select(difficulty)

    assert selection.tier is expected_tier
    assert selection.model == f"{expected_tier.value}-model"
    assert selection.reason == f"Initial routing based on task difficulty: {difficulty.value}"
