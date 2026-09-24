"""LangGraph-owned initial and continuation task turns."""

import os
import sqlite3
from pathlib import Path
from typing import Literal, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from ai_platform.events import ActorType, Event, EventType
from ai_platform.executors.base import (
    AgentActivity,
    AgentActivityType,
    AgentExecutor,
    AgentExecutorError,
    ExecutionRequest,
)
from ai_platform.models import (
    ModelSelection,
    ModelTier,
    TaskDefinition,
    TaskDifficulty,
    TaskStatus,
    VerificationStatus,
)
from ai_platform.router import ModelRouter
from ai_platform.storage import SQLiteStorage, ensure_group_writable_sqlite_files
from ai_platform.verification import build_verification_command, verify_task
from ai_platform.workspace import WorkspaceProvider

_MAX_EVENT_OUTPUT = 8000


class TaskGraphState(TypedDict, total=False):
    """Serializable high-level state shared by task-turn graph nodes."""

    task_id: str
    title: str
    description: str
    difficulty: str
    acceptance_criteria: list[str]
    workspace_path: str
    selected_tier: str
    selected_model: str
    selection_reason: str
    attempt: int
    turn_attempt: int
    tier_attempt: int
    execution_id: str
    continuation: bool
    human_messages: list[str]
    recent_agent_messages: list[str]
    human_workspace_changed: bool
    agent_succeeded: bool
    execution_cancelled: bool
    agent_error: str
    fatal_error: str
    verification_passed: bool
    verification_output: str
    status: str


def run_task_graph(
    task: TaskDefinition,
    router: ModelRouter,
    storage: SQLiteStorage,
    workspace_provider: WorkspaceProvider,
    executor: AgentExecutor,
    checkpoint_db_path: Path,
    *,
    verification_timeout_seconds: int,
    max_attempts_per_tier: int | None = None,
    max_attempts: int | None = None,
    execution_id: str | None = None,
    actor_id: str = "local-cli-user",
    continuation: bool = False,
    human_messages: list[str] | None = None,
    recent_agent_messages: list[str] | None = None,
    human_workspace_changed: bool = False,
    workspace_path: Path | None = None,
) -> TaskGraphState:
    """Run one bounded initial or continuation turn for a durable TaskSession."""

    checkpoint_db_path.parent.mkdir(parents=True, exist_ok=True)
    attempts_per_tier = max_attempts_per_tier or max_attempts or 2
    current_execution_id = execution_id or str(uuid4())
    resolved_workspace = (workspace_path or workspace_provider.get_path(task.id)).resolve()
    if resolved_workspace != workspace_provider.get_path(task.id).resolve():
        raise ValueError("Resolved task workspace does not match the workspace provider")
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    connection = sqlite3.connect(checkpoint_db_path, check_same_thread=False)
    ensure_group_writable_sqlite_files(checkpoint_db_path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        ensure_group_writable_sqlite_files(checkpoint_db_path)
        checkpointer = SqliteSaver(connection)
        graph = _build_graph(
            task,
            router,
            storage,
            workspace_provider,
            executor,
            checkpointer,
            verification_timeout_seconds,
            attempts_per_tier,
            actor_id,
            continuation,
            current_execution_id,
            resolved_workspace,
        )
        initial_state: TaskGraphState = {
            "task_id": task.id,
            "attempt": storage.get_task(task.id).attempt if continuation else 0,
            "turn_attempt": 0,
            "tier_attempt": 0,
            "execution_id": current_execution_id,
            "continuation": continuation,
            "human_messages": human_messages or [],
            "recent_agent_messages": recent_agent_messages or [],
            "human_workspace_changed": human_workspace_changed,
            "workspace_path": str(resolved_workspace),
            "agent_succeeded": False,
            "execution_cancelled": False,
            "agent_error": "",
            "fatal_error": "",
            "verification_passed": False,
            "verification_output": "",
        }
        config = {
            "configurable": {
                "thread_id": task.id,
                "checkpoint_ns": f"execution-{uuid4()}",
            }
        }
        return graph.invoke(initial_state, config=config)
    finally:
        ensure_group_writable_sqlite_files(checkpoint_db_path)
        connection.close()


def _build_graph(
    task: TaskDefinition,
    router: ModelRouter,
    storage: SQLiteStorage,
    workspace_provider: WorkspaceProvider,
    executor: AgentExecutor,
    checkpointer: SqliteSaver,
    verification_timeout_seconds: int,
    max_attempts_per_tier: int,
    actor_id: str,
    continuation: bool,
    execution_id: str,
    resolved_workspace: Path,
):
    def load_task(_state: TaskGraphState) -> TaskGraphState:
        if continuation:
            record = storage.get_task(task.id)
            if (
                record is None
                or not record.workspace_path
                or record.selected_tier is None
                or not record.selected_model
                or not workspace_provider.exists(task.id)
            ):
                return {"fatal_error": "Task continuation state or workspace is missing"}
            _change_status(
                storage,
                task.id,
                TaskStatus.ANALYZING,
                "task-graph",
                execution_id,
            )
            return {
                "task_id": task.id,
                "title": task.title,
                "description": task.description,
                "difficulty": task.difficulty.value,
                "acceptance_criteria": task.acceptance_criteria,
                "workspace_path": str(resolved_workspace),
                "selected_tier": record.selected_tier.value,
                "selected_model": record.selected_model,
                "selection_reason": "Continue with the TaskSession's selected model tier",
                "attempt": record.attempt,
                "status": TaskStatus.ANALYZING.value,
                "fatal_error": "",
            }
        storage.append_event(
            Event(
                task_id=task.id,
                event_type=EventType.TASK_STARTED,
                actor_type=ActorType.HUMAN,
                actor_id=actor_id,
                metadata={"execution_id": execution_id},
            )
        )
        _change_status(
            storage,
            task.id,
            TaskStatus.ANALYZING,
            "task-graph",
            execution_id,
        )
        return {
            "task_id": task.id,
            "title": task.title,
            "description": task.description,
            "difficulty": task.difficulty.value,
            "acceptance_criteria": task.acceptance_criteria,
            "status": TaskStatus.ANALYZING.value,
        }

    def prepare_workspace(_state: TaskGraphState) -> TaskGraphState:
        try:
            record = storage.get_task(task.id)
            if record and record.repository_id and workspace_provider.exists(task.id):
                workspace = resolved_workspace
            else:
                workspace = workspace_provider.create(task.id)
                storage.update_workspace_path(task.id, workspace)
                storage.append_event(
                    Event(
                        task_id=task.id,
                        event_type=EventType.WORKSPACE_CREATED,
                        actor_type=ActorType.SYSTEM,
                        actor_id="local-workspace",
                        metadata={"execution_id": execution_id, "path": str(workspace)},
                    )
                )
            return {"workspace_path": str(workspace), "fatal_error": ""}
        except Exception as error:
            return {"fatal_error": _safe_error(error)}

    def select_model(state: TaskGraphState) -> TaskGraphState:
        selection = router.select(TaskDifficulty(state["difficulty"]))
        _record_selection(storage, task, selection, execution_id)
        return {
            "selected_tier": selection.tier.value,
            "selected_model": selection.model,
            "selection_reason": selection.reason,
            "tier_attempt": 0,
        }

    def escalate_model(state: TaskGraphState) -> TaskGraphState:
        previous = ModelSelection(
            tier=ModelTier(state["selected_tier"]),
            model=state["selected_model"],
            reason=state["selection_reason"],
        )
        failed_attempt_count = state.get("tier_attempt", 0)
        selection = router.escalate(previous.tier, failed_attempt_count)
        if selection is None:
            return {"fatal_error": "Strong model tier exhausted", "tier_attempt": 0}
        storage.update_model_selection(task.id, selection)
        storage.append_event(
            Event(
                task_id=task.id,
                event_type=EventType.MODEL_ESCALATED,
                actor_type=ActorType.SYSTEM,
                actor_id="model-router",
                metadata={
                    "execution_id": execution_id,
                    "previous_tier": previous.tier.value,
                    "previous_model": previous.model,
                    "new_tier": selection.tier.value,
                    "new_model": selection.model,
                    "reason": selection.reason,
                    "failed_attempt_count": failed_attempt_count,
                },
            )
        )
        return {
            "selected_tier": selection.tier.value,
            "selected_model": selection.model,
            "selection_reason": selection.reason,
            "tier_attempt": 0,
        }

    def analyze_implement(state: TaskGraphState) -> TaskGraphState:
        attempt = state.get("attempt", 0) + 1
        turn_attempt = state.get("turn_attempt", 0) + 1
        tier_attempt = state.get("tier_attempt", 0) + 1
        storage.update_attempt(task.id, attempt)
        _change_status(
            storage,
            task.id,
            TaskStatus.IMPLEMENTING,
            "task-graph",
            execution_id,
        )
        try:
            preflight = executor.preflight()
            storage.append_event(
                Event(
                    task_id=task.id,
                    event_type=EventType.AGENT_STARTED,
                    actor_type=ActorType.AGENT,
                    actor_id=preflight.provider,
                    metadata={
                        "attempt": attempt,
                        "turn_attempt": turn_attempt,
                        "tier_attempt": tier_attempt,
                        "execution_id": execution_id,
                        "continuation": state.get("continuation", False),
                        "tier": state["selected_tier"],
                        "model": state["selected_model"],
                        "sdk_version": preflight.sdk_version,
                        "authentication_method": preflight.authentication_method,
                    },
                )
            )
            result = executor.execute(
                ExecutionRequest(
                    task=task,
                    selection=ModelSelection(
                        tier=state["selected_tier"],
                        model=state["selected_model"],
                        reason=state["selection_reason"],
                    ),
                    workspace_path=Path(state["workspace_path"]),
                    execution_id=execution_id,
                    attempt=attempt,
                    previous_failure=state.get("verification_output") or None,
                    continuation=state.get("continuation", False),
                    human_messages=state.get("human_messages", []),
                    recent_agent_messages=state.get("recent_agent_messages", []),
                    current_verification=(
                        storage.get_task(task.id).verification_status.value
                        if storage.get_task(task.id)
                        else VerificationStatus.NOT_RUN.value
                    ),
                    workspace_diff=workspace_provider.get_diff(task.id)[-_MAX_EVENT_OUTPUT:],
                    human_workspace_changed=state.get("human_workspace_changed", False),
                    cancellation_requested=lambda: storage.is_pause_requested(task.id),
                )
            )
            for activity in result.activities:
                _persist_activity(storage, task.id, attempt, execution_id, activity)
            event_type = EventType.AGENT_COMPLETED if result.succeeded else EventType.AGENT_FAILED
            storage.append_event(
                Event(
                    task_id=task.id,
                    event_type=event_type,
                    actor_type=ActorType.AGENT,
                    actor_id=preflight.provider,
                    metadata={
                        "attempt": attempt,
                        "execution_id": execution_id,
                        "summary": result.summary[:_MAX_EVENT_OUTPUT],
                        "session_id": result.session_id,
                        "error": result.error,
                        "fatal": result.fatal,
                        "cancelled": result.cancelled,
                        **result.usage,
                    },
                )
            )
            _record_file_changes(
                storage, workspace_provider, task.id, attempt, execution_id
            )
            return {
                "attempt": attempt,
                "turn_attempt": turn_attempt,
                "tier_attempt": tier_attempt,
                "agent_succeeded": result.succeeded,
                "execution_cancelled": result.cancelled,
                "agent_error": result.error or "",
                "fatal_error": result.error if result.fatal and result.error else "",
            }
        except AgentExecutorError as error:
            message = _safe_error(error)
            storage.append_event(
                Event(
                    task_id=task.id,
                    event_type=EventType.AGENT_FAILED,
                    actor_type=ActorType.AGENT,
                    actor_id="executor",
                    metadata={
                        "attempt": attempt,
                        "execution_id": execution_id,
                        "error": message,
                        "fatal": error.fatal,
                    },
                )
            )
            _record_file_changes(
                storage, workspace_provider, task.id, attempt, execution_id
            )
            return {
                "attempt": attempt,
                "turn_attempt": turn_attempt,
                "tier_attempt": tier_attempt,
                "agent_succeeded": False,
                "execution_cancelled": False,
                "agent_error": message,
                "fatal_error": message if error.fatal else "",
            }
        except Exception as error:
            message = _safe_error(error)
            storage.append_event(
                Event(
                    task_id=task.id,
                    event_type=EventType.AGENT_FAILED,
                    actor_type=ActorType.AGENT,
                    actor_id="executor",
                    metadata={
                        "attempt": attempt,
                        "execution_id": execution_id,
                        "error": message,
                        "fatal": True,
                    },
                )
            )
            return {
                "attempt": attempt,
                "turn_attempt": turn_attempt,
                "tier_attempt": tier_attempt,
                "agent_succeeded": False,
                "execution_cancelled": False,
                "agent_error": message,
                "fatal_error": message,
            }

    def verify(state: TaskGraphState) -> TaskGraphState:
        attempt = state["attempt"]
        _change_status(
            storage,
            task.id,
            TaskStatus.VERIFYING,
            "task-graph",
            execution_id,
        )
        command = build_verification_command(task)
        storage.append_event(
            Event(
                task_id=task.id,
                event_type=EventType.TEST_STARTED,
                actor_type=ActorType.SYSTEM,
                actor_id="pytest-verifier",
                metadata={
                    "attempt": attempt,
                    "execution_id": execution_id,
                    "command": command,
                },
            )
        )
        try:
            result = verify_task(task, Path(state["workspace_path"]), verification_timeout_seconds)
            status = VerificationStatus.PASSED if result.passed else VerificationStatus.FAILED
            storage.update_verification_status(task.id, status)
            storage.append_event(
                Event(
                    task_id=task.id,
                    event_type=(EventType.TEST_PASSED if result.passed else EventType.TEST_FAILED),
                    actor_type=ActorType.SYSTEM,
                    actor_id="pytest-verifier",
                    metadata={
                        "attempt": attempt,
                        "execution_id": execution_id,
                        "command": result.command,
                        "started_at": result.started_at.isoformat(),
                        "finished_at": result.finished_at.isoformat(),
                        "exit_code": result.exit_code,
                        "duration_seconds": round(result.duration_seconds, 3),
                        "timed_out": result.timed_out,
                        "stdout": result.stdout[-_MAX_EVENT_OUTPUT:],
                        "stderr": result.stderr[-_MAX_EVENT_OUTPUT:],
                    },
                )
            )
            failure_output = "\n".join(part for part in (result.stdout, result.stderr) if part)
            return {
                "verification_passed": result.passed,
                "verification_output": failure_output[-_MAX_EVENT_OUTPUT:],
            }
        except Exception as error:
            message = _safe_error(error)
            storage.update_verification_status(task.id, VerificationStatus.FAILED)
            storage.append_event(
                Event(
                    task_id=task.id,
                    event_type=EventType.TEST_FAILED,
                    actor_type=ActorType.SYSTEM,
                    actor_id="pytest-verifier",
                    metadata={
                        "attempt": attempt,
                        "execution_id": execution_id,
                        "command": command,
                        "error": message,
                    },
                )
            )
            return {"verification_passed": False, "verification_output": message}

    def waiting_for_human(_state: TaskGraphState) -> TaskGraphState:
        _change_status(
            storage,
            task.id,
            TaskStatus.WAITING_FOR_HUMAN,
            "task-graph",
            execution_id,
        )
        return {"status": TaskStatus.WAITING_FOR_HUMAN.value}

    def paused(_state: TaskGraphState) -> TaskGraphState:
        _change_status(
            storage,
            task.id,
            TaskStatus.PAUSED_BY_HUMAN,
            "task-graph",
            execution_id,
        )
        return {"status": TaskStatus.PAUSED_BY_HUMAN.value}

    def failed(state: TaskGraphState) -> TaskGraphState:
        _change_status(
            storage,
            task.id,
            TaskStatus.FAILED,
            "task-graph",
            execution_id,
        )
        error = state.get("fatal_error") or state.get("verification_output") or "Task failed"
        storage.append_event(
            Event(
                task_id=task.id,
                event_type=EventType.TASK_FAILED,
                actor_type=ActorType.SYSTEM,
                actor_id="task-graph",
                metadata={
                    "attempt": state.get("attempt", 0),
                    "execution_id": execution_id,
                    "error": error[-_MAX_EVENT_OUTPUT:],
                },
            )
        )
        return {"status": TaskStatus.FAILED.value}

    def after_load(
        state: TaskGraphState,
    ) -> Literal["prepare_workspace", "analyze_implement", "failed"]:
        if state.get("fatal_error"):
            return "failed"
        return "analyze_implement" if state.get("continuation") else "prepare_workspace"

    def after_prepare(state: TaskGraphState) -> Literal["select_model", "failed"]:
        return "failed" if state.get("fatal_error") else "select_model"

    def after_implementation(state: TaskGraphState) -> Literal["verify", "paused", "failed"]:
        if state.get("execution_cancelled") or storage.is_pause_requested(task.id):
            return "paused"
        return "failed" if state.get("fatal_error") else "verify"

    def after_verification(
        state: TaskGraphState,
    ) -> Literal[
        "waiting_for_human", "analyze_implement", "escalate_model", "paused", "failed"
    ]:
        if storage.is_pause_requested(task.id):
            return "paused"
        if state.get("verification_passed"):
            return "waiting_for_human"
        if state.get("tier_attempt", 0) < max_attempts_per_tier:
            return "analyze_implement"
        if router.next_tier(ModelTier(state["selected_tier"])) is not None:
            return "escalate_model"
        return "failed"

    builder = StateGraph(TaskGraphState)
    builder.add_node("load_task", load_task)
    builder.add_node("prepare_workspace", prepare_workspace)
    builder.add_node("select_model", select_model)
    builder.add_node("escalate_model", escalate_model)
    builder.add_node("analyze_implement", analyze_implement)
    builder.add_node("verify", verify)
    builder.add_node("waiting_for_human", waiting_for_human)
    builder.add_node("paused", paused)
    builder.add_node("failed", failed)
    builder.add_edge(START, "load_task")
    builder.add_conditional_edges("load_task", after_load)
    builder.add_conditional_edges("prepare_workspace", after_prepare)
    builder.add_edge("select_model", "analyze_implement")
    builder.add_conditional_edges("analyze_implement", after_implementation)
    builder.add_conditional_edges("verify", after_verification)
    builder.add_edge("escalate_model", "analyze_implement")
    builder.add_edge("waiting_for_human", END)
    builder.add_edge("paused", END)
    builder.add_edge("failed", END)
    return builder.compile(checkpointer=checkpointer)


def _record_selection(
    storage: SQLiteStorage,
    task: TaskDefinition,
    selection: ModelSelection,
    execution_id: str,
) -> None:
    storage.update_model_selection(task.id, selection)
    storage.append_event(
        Event(
            task_id=task.id,
            event_type=EventType.MODEL_SELECTED,
            actor_type=ActorType.SYSTEM,
            actor_id="model-router",
            metadata={
                "execution_id": execution_id,
                "difficulty": task.difficulty.value,
                **selection.model_dump(mode="json"),
            },
        )
    )


def _change_status(
    storage: SQLiteStorage,
    task_id: str,
    status: TaskStatus,
    actor_id: str,
    execution_id: str,
) -> None:
    current = storage.get_task(task_id)
    previous = current.status if current else None
    storage.update_task_status(task_id, status)
    storage.append_event(
        Event(
            task_id=task_id,
            event_type=EventType.STATUS_CHANGED,
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            metadata={
                "execution_id": execution_id,
                "from": previous.value if previous else None,
                "to": status.value,
            },
        )
    )


def _persist_activity(
    storage: SQLiteStorage,
    task_id: str,
    attempt: int,
    execution_id: str,
    activity: AgentActivity,
) -> None:
    event_type = (
        EventType.AGENT_MESSAGE
        if activity.activity_type is AgentActivityType.MESSAGE
        else EventType.AGENT_TOOL_ACTIVITY
    )
    storage.append_event(
        Event(
            task_id=task_id,
            event_type=event_type,
            actor_type=ActorType.AGENT,
            actor_id="claude",
            metadata={
                "attempt": attempt,
                "execution_id": execution_id,
                "activity": activity.activity_type.value,
                **activity.metadata,
            },
        )
    )


def _record_file_changes(
    storage: SQLiteStorage,
    workspace_provider: WorkspaceProvider,
    task_id: str,
    attempt: int,
    execution_id: str,
) -> None:
    for change in workspace_provider.get_changed_files(task_id):
        storage.append_event(
            Event(
                task_id=task_id,
                event_type=EventType.FILE_CHANGED,
                actor_type=ActorType.SYSTEM,
                actor_id="git-inspector",
                metadata={
                    "attempt": attempt,
                    "execution_id": execution_id,
                    "path": change.path,
                    "change_type": change.change_type,
                },
            )
        )


def _safe_error(error: Exception) -> str:
    return (str(error) or type(error).__name__)[:2000]
