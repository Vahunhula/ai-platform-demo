"""Phase 2.1 logical model catalog, preferences, resolution, and audit tests."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_platform.api.app import create_app
from ai_platform.auth import AuthenticatedUser, Role
from ai_platform.events import EventType
from ai_platform.model_preferences import ModelPreferenceService
from ai_platform.models import LogicalModel, ModelResolutionSource, TaskDefinition, TaskDifficulty
from ai_platform.router import ModelCatalog, ModelCatalogEntry, ModelRouter, ModelUnavailableError
from ai_platform.storage import SQLiteStorage
from ai_platform.workflow import WorkflowPhase
from tests.test_messaging import _client, _context


def _catalog(*, opus: bool = True) -> ModelCatalog:
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
                configured_model_id="sonnet-id",
                enabled=True,
            ),
            LogicalModel.CLAUDE_OPUS: ModelCatalogEntry(
                logical_id=LogicalModel.CLAUDE_OPUS,
                display_name="Claude Opus",
                provider="anthropic",
                configured_model_id="opus-id" if opus else None,
                enabled=opus,
            ),
        }
    )


def _setup(tmp_path: Path, *ids: str):
    storage = SQLiteStorage(tmp_path / "routing.db")
    storage.initialize()
    for task_id in ids or ("T-1",):
        storage.create_task(
            TaskDefinition(
                id=task_id,
                title="Route",
                description="Route it",
                difficulty=TaskDifficulty.MEDIUM,
                acceptance_criteria=["Works"],
                verification={"type": "pytest", "targets": ["tests"]},
            )
        )
    catalog = _catalog()
    router = ModelRouter({}, catalog, storage)
    return storage, catalog, router


def _user(role: Role = Role.DEVELOPER) -> AuthenticatedUser:
    return AuthenticatedUser("u-1", "dev", "Developer", role)


def test_defaults_policy_precedence_and_audit(tmp_path: Path) -> None:
    storage, catalog, router = _setup(tmp_path)
    service = ModelPreferenceService(storage, catalog)
    assert storage.get_task("T-1").default_model_selection is LogicalModel.AUTO
    initial = router.resolve("T-1", WorkflowPhase.BRAINSTORM)
    assert (initial.effective_selection, initial.resolution_source) == (
        LogicalModel.CLAUDE_SONNET,
        ModelResolutionSource.AUTO_POLICY,
    )

    assert service.set_default("T-1", LogicalModel.CLAUDE_OPUS, _user())
    inherited = router.resolve("T-1", WorkflowPhase.PLAN)
    assert inherited.model == "opus-id"
    assert inherited.resolution_source is ModelResolutionSource.TASK_DEFAULT
    assert service.set_phase("T-1", WorkflowPhase.PLAN, LogicalModel.CLAUDE_SONNET, _user())
    overridden = router.resolve("T-1", WorkflowPhase.PLAN)
    assert overridden.model == "sonnet-id"
    assert overridden.resolution_source is ModelResolutionSource.PHASE_OVERRIDE
    assert service.set_phase("T-1", WorkflowPhase.PLAN, LogicalModel.AUTO, _user())
    assert router.resolve("T-1", WorkflowPhase.PLAN).model == "opus-id"

    events = storage.get_events("T-1")
    assert EventType.TASK_MODEL_DEFAULT_CHANGED in {event.event_type for event in events}
    assert EventType.PHASE_MODEL_OVERRIDE_CHANGED in {event.event_type for event in events}
    assert (
        next(e for e in events if e.event_type is EventType.TASK_MODEL_DEFAULT_CHANGED).actor_id
        == "dev"
    )


def test_unavailable_and_human_review_are_rejected(tmp_path: Path) -> None:
    storage, _, _ = _setup(tmp_path)
    catalog = _catalog(opus=False)
    service = ModelPreferenceService(storage, catalog)
    with pytest.raises(ModelUnavailableError):
        service.set_default("T-1", LogicalModel.CLAUDE_OPUS, _user())
    with pytest.raises(Exception, match="HUMAN_REVIEW"):
        service.set_phase("T-1", WorkflowPhase.HUMAN_REVIEW, LogicalModel.AUTO, _user())


def test_resolved_turn_snapshot_is_immutable_when_future_preference_changes(
    tmp_path: Path,
) -> None:
    storage, catalog, router = _setup(tmp_path)
    current_turn = router.resolve("T-1", WorkflowPhase.IMPLEMENTATION)
    ModelPreferenceService(storage, catalog).set_phase(
        "T-1", WorkflowPhase.IMPLEMENTATION, LogicalModel.CLAUDE_OPUS, _user()
    )
    next_turn = router.resolve("T-1", WorkflowPhase.IMPLEMENTATION)
    assert current_turn.model == "sonnet-id"
    assert next_turn.model == "opus-id"


def test_concurrent_preference_updates_are_serialized_and_tasks_are_isolated(
    tmp_path: Path,
) -> None:
    storage, catalog, _ = _setup(tmp_path, "T-1", "T-2")
    service = ModelPreferenceService(storage, catalog)
    barrier = threading.Barrier(2)

    def update(selection: LogicalModel) -> None:
        barrier.wait()
        service.set_default("T-1", selection, _user())

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(update, [LogicalModel.CLAUDE_SONNET, LogicalModel.CLAUDE_OPUS]))
    assert storage.get_task("T-1").default_model_selection in {
        LogicalModel.CLAUDE_SONNET,
        LogicalModel.CLAUDE_OPUS,
    }
    assert storage.get_task("T-2").default_model_selection is LogicalModel.AUTO
    changes = [
        event
        for event in storage.get_events("T-1")
        if event.event_type is EventType.TASK_MODEL_DEFAULT_CHANGED
    ]
    assert len(changes) == 2


@pytest.mark.anyio
async def test_model_routing_api_is_safe_authorized_and_rejects_actor_spoof(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    app = create_app(context)
    async with _client(app, "watcher", Role.VIEWER) as viewer:
        catalog = await viewer.get("/api/models")
        routing = await viewer.get("/api/tasks/DEMO-1/model-routing")
        forbidden = await viewer.put(
            "/api/tasks/DEMO-1/model-routing/default",
            json={"selection": "CLAUDE_OPUS"},
        )
    assert catalog.status_code == 200
    assert set(catalog.json()[0]) == {"logical_id", "display_name", "provider", "enabled"}
    assert {phase["selection"] for phase in routing.json()["phases"]} == {"AUTO"}
    assert forbidden.status_code == 403

    async with _client(app, "alice") as developer:
        spoof = await developer.put(
            "/api/tasks/DEMO-1/model-routing/default",
            json={"selection": "CLAUDE_OPUS", "updated_by": "mallory"},
        )
        changed = await developer.put(
            "/api/tasks/DEMO-1/model-routing/default",
            json={"selection": "CLAUDE_OPUS"},
        )
        human_review = await developer.put(
            "/api/tasks/DEMO-1/model-routing/phases/HUMAN_REVIEW",
            json={"selection": "AUTO"},
        )
    assert spoof.status_code == 422
    assert changed.status_code == 200
    assert changed.json()["applies_to_next_turn"] is True
    assert human_review.status_code == 400
    event = next(
        item
        for item in context.storage.get_events("DEMO-1")
        if item.event_type is EventType.TASK_MODEL_DEFAULT_CHANGED
    )
    assert event.actor_id == "alice"
