"""Minimal LangGraph workflow for loading a task and selecting a model."""

import os
import sqlite3
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from ai_platform.events import ActorType, Event, EventType
from ai_platform.models import ModelSelection, TaskDefinition, TaskDifficulty, TaskStatus
from ai_platform.router import ModelRouter
from ai_platform.storage import SQLiteStorage


class TaskGraphState(TypedDict, total=False):
    """Serializable state shared by the foundation graph nodes."""

    task_id: str
    title: str
    description: str
    difficulty: str
    acceptance_criteria: list[str]
    selected_tier: str
    selected_model: str
    selection_reason: str
    status: str


def run_task_graph(
    task: TaskDefinition,
    router: ModelRouter,
    storage: SQLiteStorage,
    checkpoint_db_path: Path,
) -> TaskGraphState:
    """Run the minimal persistent graph using the task ID as LangGraph thread ID."""

    checkpoint_db_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    connection = sqlite3.connect(checkpoint_db_path, check_same_thread=False)
    try:
        checkpointer = SqliteSaver(connection)
        graph = _build_graph(task, router, storage, checkpointer)
        initial_state: TaskGraphState = {"task_id": task.id}
        config = {"configurable": {"thread_id": task.id}}
        return graph.invoke(initial_state, config=config)
    finally:
        connection.close()


def _build_graph(
    task: TaskDefinition,
    router: ModelRouter,
    storage: SQLiteStorage,
    checkpointer: SqliteSaver,
):
    def load_task(_state: TaskGraphState) -> TaskGraphState:
        return {
            "task_id": task.id,
            "title": task.title,
            "description": task.description,
            "difficulty": task.difficulty.value,
            "acceptance_criteria": task.acceptance_criteria,
        }

    def select_model(state: TaskGraphState) -> TaskGraphState:
        selection = router.select(TaskDifficulty(state["difficulty"]))
        _record_selection(storage, task.id, selection)
        return {
            "selected_tier": selection.tier.value,
            "selected_model": selection.model,
            "selection_reason": selection.reason,
        }

    def ready(_state: TaskGraphState) -> TaskGraphState:
        storage.update_task_status(task.id, TaskStatus.READY)
        storage.append_event(
            Event(
                task_id=task.id,
                event_type=EventType.STATUS_CHANGED,
                actor_type=ActorType.SYSTEM,
                actor_id="foundation-graph",
                metadata={"status": TaskStatus.READY.value},
            )
        )
        return {"status": TaskStatus.READY.value}

    builder = StateGraph(TaskGraphState)
    builder.add_node("load_task", load_task)
    builder.add_node("select_model", select_model)
    builder.add_node("ready", ready)
    builder.add_edge(START, "load_task")
    builder.add_edge("load_task", "select_model")
    builder.add_edge("select_model", "ready")
    builder.add_edge("ready", END)
    return builder.compile(checkpointer=checkpointer)


def _record_selection(storage: SQLiteStorage, task_id: str, selection: ModelSelection) -> None:
    storage.update_model_selection(task_id, selection)
    storage.append_event(
        Event(
            task_id=task_id,
            event_type=EventType.MODEL_SELECTED,
            actor_type=ActorType.SYSTEM,
            actor_id="model-router",
            metadata=selection.model_dump(mode="json"),
        )
    )
