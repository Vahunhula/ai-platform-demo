"""Tests for task-file loading and validation."""

import json
from pathlib import Path

import pytest

from ai_platform.models import TaskDifficulty
from ai_platform.task_loader import get_task, load_tasks


def test_loads_demo_tasks() -> None:
    tasks_path = Path(__file__).parents[1] / "tasks.json"

    tasks = load_tasks(tasks_path)

    assert [task.id for task in tasks] == ["DEMO-1", "DEMO-2", "DEMO-3"]
    assert get_task(tasks, "demo-2").difficulty is TaskDifficulty.MEDIUM


def test_rejects_duplicate_task_ids(tmp_path: Path) -> None:
    task = {
        "id": "DUPLICATE",
        "title": "Duplicate",
        "description": "Duplicate task",
        "difficulty": "low",
        "acceptance_criteria": ["Rejected"],
    }
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(json.dumps([task, task]), encoding="utf-8")

    with pytest.raises(ValueError, match="Task IDs must be unique"):
        load_tasks(tasks_path)
