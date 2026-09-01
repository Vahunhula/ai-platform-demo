"""Load and validate task definitions."""

import json
from pathlib import Path

from pydantic import TypeAdapter

from ai_platform.models import TaskDefinition

_TASK_LIST = TypeAdapter(list[TaskDefinition])


def load_tasks(path: Path) -> list[TaskDefinition]:
    """Load a JSON task list and reject duplicate IDs."""

    with path.open(encoding="utf-8") as task_file:
        tasks = _TASK_LIST.validate_python(json.load(task_file))

    ids = [task.id for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("Task IDs must be unique")
    return tasks


def get_task(tasks: list[TaskDefinition], task_id: str) -> TaskDefinition:
    """Return one task by ID, ignoring ID letter case."""

    normalized_id = task_id.upper()
    for task in tasks:
        if task.id.upper() == normalized_id:
            return task
    raise KeyError(task_id)
