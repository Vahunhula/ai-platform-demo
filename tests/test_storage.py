"""Tests for durable task state and append-only events."""

from pathlib import Path

from ai_platform.events import ActorType, Event, EventType
from ai_platform.models import TaskDefinition, TaskDifficulty, TaskStatus
from ai_platform.storage import SQLiteStorage


def _task() -> TaskDefinition:
    return TaskDefinition(
        id="TEST-1",
        title="Test task",
        description="Exercise persistence",
        difficulty=TaskDifficulty.LOW,
        acceptance_criteria=["State survives a new storage instance"],
        verification={"type": "pytest", "targets": ["tests/test_example.py"]},
    )


def test_persists_task_state_across_storage_instances(tmp_path: Path) -> None:
    db_path = tmp_path / "platform.db"
    storage = SQLiteStorage(db_path)
    storage.initialize()
    assert storage.create_task(_task()) is True
    assert storage.create_task(_task()) is False
    storage.update_task_status("TEST-1", TaskStatus.ANALYZING)

    reopened = SQLiteStorage(db_path)
    reopened.initialize()
    task = reopened.get_task("TEST-1")

    assert task is not None
    assert task.status is TaskStatus.ANALYZING


def test_appends_events_in_order(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "platform.db")
    storage.initialize()
    storage.create_task(_task())
    created = Event(
        task_id="TEST-1",
        event_type=EventType.TASK_CREATED,
        actor_type=ActorType.SYSTEM,
        actor_id="test",
    )
    started = Event(
        task_id="TEST-1",
        event_type=EventType.TASK_STARTED,
        actor_type=ActorType.HUMAN,
        actor_id="tester",
    )

    storage.append_event(created)
    storage.append_event(started)

    assert storage.get_events("TEST-1") == [created, started]
