"""LangGraph-owned initial and continuation task turns.

Phase 3: the graph is phase-aware. One durable ``TaskSession`` maps to one
LangGraph thread (``thread_id = task_id``); a single invocation walks forward
through as many workflow phases as their readiness gates allow in one turn:

    prepare -> brainstorm -> [gate] -> plan -> [gate] -> implementation ->
    verify -> [gate] -> review -> [gate] -> human_review

Each phase resolves its own model, builds its own bounded context from durable
artifacts (never a raw prior-phase transcript), and is a fresh executor
invocation. BRAINSTORM, PLAN and REVIEW are enforced read-only at the tool
layer (see ``executors.claude``) and re-checked here by diffing a workspace
snapshot taken immediately before and after the phase's agent call; an
unexpected mutation fails that phase's gate closed rather than being trusted.

Readiness/eligibility is always computed by the platform (``workflow.py``),
never self-declared by the model.
"""

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
    ExecutionResult,
)
from ai_platform.models import (
    ModelSelection,
    TaskDefinition,
    TaskStatus,
    VerificationStatus,
)
from ai_platform.router import ModelRouter
from ai_platform.storage import SQLiteStorage, ensure_group_writable_sqlite_files
from ai_platform.verification import (
    BaselineContext,
    build_verification_command,
    verify_registered_task,
    verify_task,
)
from ai_platform.workflow import (
    CHECKLISTS,
    ArtifactKind,
    ChecklistResult,
    ChecklistStatus,
    WorkflowPhase,
    artifact_json_schema,
    calculate_readiness,
    resolve_checklist,
    validate_artifact_payload,
)
from ai_platform.workspace import WorkspaceProvider

_MAX_EVENT_OUTPUT = 8000
_READ_ONLY_INSPECTION_TOOLS = {"Read", "Glob", "Grep"}

# Fixed pipeline order; only IMPLEMENTATION has an internal bounded retry loop.
_NEXT_PHASE = {
    WorkflowPhase.BRAINSTORM: WorkflowPhase.PLAN,
    WorkflowPhase.PLAN: WorkflowPhase.IMPLEMENTATION,
    WorkflowPhase.REVIEW: WorkflowPhase.HUMAN_REVIEW,
}
_PHASE_ARTIFACT_KIND = {
    WorkflowPhase.BRAINSTORM: ArtifactKind.BRAINSTORM_SUMMARY,
    WorkflowPhase.PLAN: ArtifactKind.PLAN,
    WorkflowPhase.REVIEW: ArtifactKind.REVIEW_REPORT,
}
_PHASE_UPSTREAM_KINDS: dict[WorkflowPhase, frozenset[ArtifactKind]] = {
    WorkflowPhase.BRAINSTORM: frozenset(),
    WorkflowPhase.PLAN: frozenset({ArtifactKind.BRAINSTORM_SUMMARY}),
    WorkflowPhase.REVIEW: frozenset(
        {ArtifactKind.BRAINSTORM_SUMMARY, ArtifactKind.PLAN, ArtifactKind.IMPLEMENTATION_SUMMARY}
    ),
}
_ENTRY_NODE_BY_PHASE = {
    WorkflowPhase.BRAINSTORM: "brainstorm",
    WorkflowPhase.PLAN: "plan",
    WorkflowPhase.IMPLEMENTATION: "select_model_implementation",
    WorkflowPhase.REVIEW: "review",
    # No agent runs in HUMAN_REVIEW; a turn that somehow starts here (the
    # session layer should never allow it) just re-confirms the wait state.
    WorkflowPhase.HUMAN_REVIEW: "waiting_for_human",
}


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
    requested_selection: str
    effective_selection: str
    model_provider: str
    resolution_source: str
    workflow_phase: str
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
    # Phase 3 transients, overwritten on each phase/attempt.
    gate_eligible: bool
    implementation_known_issues: list[str]
    implementation_agent_summary: str
    implementation_structured_valid: bool


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
    baseline_context: BaselineContext | None = None,
) -> TaskGraphState:
    """Run one bounded turn for a durable TaskSession, across as many phases as its
    readiness gates allow, starting from the task's current durable workflow phase.
    """

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
            baseline_context,
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
    baseline_context: BaselineContext | None,
):
    def load_task(_state: TaskGraphState) -> TaskGraphState:
        record = storage.get_task(task.id)
        if continuation:
            if record is None or not record.workspace_path or not workspace_provider.exists(
                task.id
            ):
                return {"fatal_error": "Task continuation state or workspace is missing"}
            _change_status(storage, task.id, TaskStatus.ANALYZING, "task-graph", execution_id)
            return {
                "task_id": task.id,
                "title": task.title,
                "description": task.description,
                "difficulty": task.difficulty.value,
                "acceptance_criteria": task.acceptance_criteria,
                "workspace_path": str(resolved_workspace),
                "attempt": record.attempt,
                "workflow_phase": record.workflow_phase.value,
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
        _change_status(storage, task.id, TaskStatus.ANALYZING, "task-graph", execution_id)
        return {
            "task_id": task.id,
            "title": task.title,
            "description": task.description,
            "difficulty": task.difficulty.value,
            "acceptance_criteria": task.acceptance_criteria,
            "workflow_phase": (record.workflow_phase.value if record else task.id),
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

    # ---- BRAINSTORM / PLAN / REVIEW: read-only phases sharing one shape -----

    def _run_readonly_phase(
        state: TaskGraphState, phase: WorkflowPhase
    ) -> TaskGraphState:
        attempt = state.get("attempt", 0) + 1
        try:
            selection = router.resolve(task.id, phase, storage=storage)
        except Exception as error:
            return {"fatal_error": _safe_error(error), "attempt": attempt}
        _record_selection(storage, task, selection, execution_id)
        storage.update_attempt(task.id, attempt)
        _change_status(storage, task.id, TaskStatus.ANALYZING, "task-graph", execution_id)
        storage.append_event(
            Event(
                task_id=task.id,
                event_type=EventType.WORKFLOW_PHASE_STARTED,
                actor_type=ActorType.SYSTEM,
                actor_id="task-graph",
                metadata={
                    "phase": phase.value,
                    "execution_id": execution_id,
                    "tier": selection.tier.value,
                    "model": selection.model,
                },
            )
        )
        kind = _PHASE_ARTIFACT_KIND[phase]
        upstream = _upstream_artifacts(storage, task.id, _PHASE_UPSTREAM_KINDS[phase])
        before_snapshot = workspace_provider.snapshot(task.id)
        request = ExecutionRequest(
            task=task,
            selection=selection,
            workspace_path=Path(state["workspace_path"]),
            execution_id=execution_id,
            attempt=attempt,
            phase=phase,
            continuation=state.get("continuation", False),
            human_messages=state.get("human_messages", []),
            current_verification=(
                storage.get_task(task.id).verification_status.value
                if storage.get_task(task.id)
                else VerificationStatus.NOT_RUN.value
            ),
            workspace_diff=workspace_provider.get_diff(task.id)[-_MAX_EVENT_OUTPUT:],
            canonical_changed_files=(
                [change.path for change in workspace_provider.get_changed_files(task.id)]
                if phase is WorkflowPhase.REVIEW
                else []
            ),
            canonical_verification=(
                _canonical_verification_evidence(storage, task.id)
                if phase is WorkflowPhase.REVIEW
                else {}
            ),
            source_commit=(baseline_context.source_commit if baseline_context else None),
            upstream_artifacts=upstream,
            output_schema=artifact_json_schema(kind),
            cancellation_requested=lambda: storage.is_pause_requested(task.id),
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
                        "execution_id": execution_id,
                        "phase": phase.value,
                        "tier": selection.tier.value,
                        "model": selection.model,
                        "sdk_version": preflight.sdk_version,
                        "authentication_method": preflight.authentication_method,
                    },
                )
            )
            result, payload, activities = _execute_structured(executor, request, kind)
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
                        "phase": phase.value,
                        "error": message,
                        "fatal": error.fatal,
                    },
                )
            )
            if error.fatal:
                return {"fatal_error": message, "attempt": attempt}
            payload, activities, result = None, [], None
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
                        "phase": phase.value,
                        "error": message,
                        "fatal": True,
                    },
                )
            )
            return {"fatal_error": message, "attempt": attempt}
        else:
            for activity in activities:
                _persist_activity(storage, task.id, attempt, execution_id, activity)
            event_type = (
                EventType.AGENT_COMPLETED
                if result is not None and result.succeeded
                else EventType.AGENT_FAILED
            )
            storage.append_event(
                Event(
                    task_id=task.id,
                    event_type=event_type,
                    actor_type=ActorType.AGENT,
                    actor_id=preflight.provider,
                    metadata={
                        "attempt": attempt,
                        "execution_id": execution_id,
                        "phase": phase.value,
                        "summary": (result.summary[:_MAX_EVENT_OUTPUT] if result else ""),
                        "error": result.error if result else "No structured result",
                        **(result.usage if result else {}),
                    },
                )
            )

        after_snapshot = workspace_provider.snapshot(task.id)
        violation = bool(workspace_provider.changes_between(before_snapshot, after_snapshot))
        if violation:
            storage.append_event(
                Event(
                    task_id=task.id,
                    event_type=EventType.WORKFLOW_PHASE_GATE_EVALUATED,
                    actor_type=ActorType.SYSTEM,
                    actor_id="workflow-gate",
                    metadata={
                        "phase": phase.value,
                        "execution_id": execution_id,
                        "error": "Unexpected workspace mutation detected during a read-only phase",
                    },
                )
            )

        artifact_created = None
        if payload is not None and not violation:
            artifact_created = storage.create_workflow_artifact(
                task.id,
                phase,
                kind,
                payload,
                created_by=f"agent:{phase.value.lower()}",
                created_by_type=ActorType.AGENT,
                execution_id=execution_id,
                logical_model=selection.effective_selection.value,
                concrete_model=selection.model,
                provider=selection.provider,
            )
            storage.append_event(
                Event(
                    task_id=task.id,
                    event_type=EventType.WORKFLOW_PHASE_OUTPUT_CREATED,
                    actor_type=ActorType.SYSTEM,
                    actor_id="task-graph",
                    metadata={
                        "phase": phase.value,
                        "execution_id": execution_id,
                        "artifact_id": artifact_created.artifact_id,
                        "kind": kind.value,
                        "version": artifact_created.version,
                    },
                )
            )

        results = _checklist_results(phase, payload, activities, violation)
        readiness = _persist_gate(storage, task.id, phase, results, execution_id)

        base_update: TaskGraphState = {
            "attempt": attempt,
            "selected_tier": selection.tier.value,
            "selected_model": selection.model,
            "selection_reason": selection.reason,
            "requested_selection": selection.requested_selection.value,
            "effective_selection": selection.effective_selection.value,
            "model_provider": selection.provider,
            "resolution_source": selection.resolution_source.value,
            "fatal_error": "",
        }
        if readiness.eligible_for_auto_progression and not violation:
            target = _NEXT_PHASE[phase]
            _transition_phase(storage, task.id, phase, target, execution_id)
            return {**base_update, "workflow_phase": target.value, "gate_eligible": True}
        storage.append_event(
            Event(
                task_id=task.id,
                event_type=EventType.WORKFLOW_PHASE_WAITING_FOR_HUMAN,
                actor_type=ActorType.SYSTEM,
                actor_id="task-graph",
                metadata={
                    "phase": phase.value,
                    "execution_id": execution_id,
                    "score": readiness.score,
                    "blocking_failures": readiness.blocking_failures,
                    "blocking_needs_human": readiness.blocking_needs_human,
                },
            )
        )
        return {**base_update, "workflow_phase": phase.value, "gate_eligible": False}

    def brainstorm(state: TaskGraphState) -> TaskGraphState:
        return _run_readonly_phase(state, WorkflowPhase.BRAINSTORM)

    def plan(state: TaskGraphState) -> TaskGraphState:
        return _run_readonly_phase(state, WorkflowPhase.PLAN)

    def review(state: TaskGraphState) -> TaskGraphState:
        return _run_readonly_phase(state, WorkflowPhase.REVIEW)

    # ---- IMPLEMENTATION: reuses the existing bounded retry/escalation loop --

    def select_model_implementation(_state: TaskGraphState) -> TaskGraphState:
        selection = router.resolve(task.id, WorkflowPhase.IMPLEMENTATION, storage=storage)
        _record_selection(storage, task, selection, execution_id)
        return {
            "selected_tier": selection.tier.value,
            "selected_model": selection.model,
            "selection_reason": selection.reason,
            "requested_selection": selection.requested_selection.value,
            "effective_selection": selection.effective_selection.value,
            "model_provider": selection.provider,
            "resolution_source": selection.resolution_source.value,
            "workflow_phase": WorkflowPhase.IMPLEMENTATION.value,
            "tier_attempt": 0,
        }

    def escalate_model(state: TaskGraphState) -> TaskGraphState:
        previous = _selection_from_state(state)
        failed_attempt_count = state.get("tier_attempt", 0)
        selection = router.escalate_selection(previous, failed_attempt_count)
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
                    "requested_selection": selection.requested_selection.value,
                    "effective_selection": selection.effective_selection.value,
                    "provider": selection.provider,
                    "resolution_source": selection.resolution_source.value,
                    "workflow_phase": selection.workflow_phase.value,
                },
            )
        )
        return {
            "selected_tier": selection.tier.value,
            "selected_model": selection.model,
            "selection_reason": selection.reason,
            "requested_selection": selection.requested_selection.value,
            "effective_selection": selection.effective_selection.value,
            "model_provider": selection.provider,
            "resolution_source": selection.resolution_source.value,
            "workflow_phase": selection.workflow_phase.value,
            "tier_attempt": 0,
        }

    def analyze_implement(state: TaskGraphState) -> TaskGraphState:
        attempt = state.get("attempt", 0) + 1
        turn_attempt = state.get("turn_attempt", 0) + 1
        tier_attempt = state.get("tier_attempt", 0) + 1
        storage.update_attempt(task.id, attempt)
        _change_status(storage, task.id, TaskStatus.IMPLEMENTING, "task-graph", execution_id)
        upstream = _upstream_artifacts(
            storage,
            task.id,
            frozenset({ArtifactKind.BRAINSTORM_SUMMARY, ArtifactKind.PLAN}),
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
                        "requested_selection": state["requested_selection"],
                        "effective_selection": state["effective_selection"],
                        "provider": state["model_provider"],
                        "resolution_source": state["resolution_source"],
                        "workflow_phase": WorkflowPhase.IMPLEMENTATION.value,
                        "sdk_version": preflight.sdk_version,
                        "authentication_method": preflight.authentication_method,
                    },
                )
            )
            result = executor.execute(
                ExecutionRequest(
                    task=task,
                    selection=_selection_from_state(state),
                    workspace_path=Path(state["workspace_path"]),
                    execution_id=execution_id,
                    attempt=attempt,
                    phase=WorkflowPhase.IMPLEMENTATION,
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
                    upstream_artifacts=upstream,
                    output_schema=artifact_json_schema(ArtifactKind.IMPLEMENTATION_SUMMARY),
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
            _record_file_changes(storage, workspace_provider, task.id, attempt, execution_id)
            summary_payload = _validate_or_none(
                ArtifactKind.IMPLEMENTATION_SUMMARY, result.structured_output
            )
            return {
                "attempt": attempt,
                "turn_attempt": turn_attempt,
                "tier_attempt": tier_attempt,
                "agent_succeeded": result.succeeded,
                "execution_cancelled": result.cancelled,
                "agent_error": result.error or "",
                "fatal_error": result.error if result.fatal and result.error else "",
                "implementation_structured_valid": summary_payload is not None,
                "implementation_known_issues": (
                    summary_payload["known_issues"] if summary_payload else []
                ),
                "implementation_agent_summary": (
                    summary_payload["summary"] if summary_payload else result.summary
                ),
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
            _record_file_changes(storage, workspace_provider, task.id, attempt, execution_id)
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
        _change_status(storage, task.id, TaskStatus.VERIFYING, "task-graph", execution_id)
        command = build_verification_command(task)
        storage.append_event(
            Event(
                task_id=task.id,
                event_type=EventType.TEST_STARTED,
                actor_type=ActorType.SYSTEM,
                actor_id="pytest-verifier",
                metadata={"attempt": attempt, "execution_id": execution_id, "command": command},
            )
        )
        try:
            if baseline_context is None:
                result = verify_task(
                    task, Path(state["workspace_path"]), verification_timeout_seconds
                )
                verification_metadata = {
                    "verification_mode": "LEGACY_EXPLICIT",
                    "task_specific_passed": result.passed,
                    "task_specific_passed_tests": None,
                    "pre_existing_failures": [],
                    "fixed_failures": [],
                    "new_failures": [],
                    "baseline_warning_count": 0,
                    "new_regression_count": 0,
                }
                failure_output = "\n".join(
                    part for part in (result.stdout, result.stderr) if part
                )
            else:
                changed_paths = [
                    change.path for change in workspace_provider.get_changed_files(task.id)
                ]
                combined = verify_registered_task(
                    task,
                    Path(state["workspace_path"]),
                    verification_timeout_seconds,
                    baseline_context,
                    changed_paths,
                )
                result = combined.current_run
                result.passed = combined.passed
                verification_metadata = {
                    "verification_mode": "BASELINE_AWARE",
                    "task_specific_targets": combined.task_specific.targets,
                    "task_specific_passed": combined.task_specific.passed,
                    "task_specific_passed_tests": combined.task_specific.passed_tests,
                    "pre_existing_failures": combined.broad_regression.pre_existing_failures,
                    "fixed_failures": combined.broad_regression.fixed_failures,
                    "new_failures": combined.broad_regression.new_failures,
                    "baseline_warning_count": len(
                        combined.broad_regression.pre_existing_failures
                    ),
                    "new_regression_count": len(combined.broad_regression.new_failures),
                    "broad_regression_passed": combined.broad_regression.passed,
                    "baseline_cached": combined.broad_regression.baseline_cached,
                    "baseline_identity": combined.broad_regression.baseline_identity,
                    "blocking_failures": combined.blocking_failures,
                    "warnings": combined.warnings,
                }
                failure_output = combined.correction_context()
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
                        **verification_metadata,
                    },
                )
            )
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

    def implementation_gate(state: TaskGraphState) -> TaskGraphState:
        files_changed = [change.path for change in workspace_provider.get_changed_files(task.id)]
        verification_passed = bool(state.get("verification_passed"))
        canonical_verification = _canonical_verification_evidence(storage, task.id)
        known_issues = state.get("implementation_known_issues", [])
        structured_valid = bool(state.get("implementation_structured_valid"))
        results = _implementation_checklist(
            files_changed, verification_passed, known_issues, structured_valid
        )
        readiness = _persist_gate(
            storage, task.id, WorkflowPhase.IMPLEMENTATION, results, execution_id
        )
        selection = _selection_from_state(state)
        summary_text = (state.get("implementation_agent_summary") or "").strip() or (
            "Implementation attempt completed."
        )
        try:
            payload = validate_artifact_payload(
                ArtifactKind.IMPLEMENTATION_SUMMARY,
                {
                    "summary": summary_text[:20000],
                    "files_changed": files_changed,
                    "tests_run": _canonical_verification_lines(canonical_verification),
                    "known_issues": known_issues,
                    "canonical_verification": canonical_verification,
                    "source_commit": (
                        baseline_context.source_commit if baseline_context else None
                    ),
                },
            )
            artifact = storage.create_workflow_artifact(
                task.id,
                WorkflowPhase.IMPLEMENTATION,
                ArtifactKind.IMPLEMENTATION_SUMMARY,
                payload,
                created_by="agent:implementation",
                created_by_type=ActorType.AGENT,
                execution_id=execution_id,
                logical_model=selection.effective_selection.value,
                concrete_model=selection.model,
                provider=selection.provider,
            )
            storage.append_event(
                Event(
                    task_id=task.id,
                    event_type=EventType.WORKFLOW_PHASE_OUTPUT_CREATED,
                    actor_type=ActorType.SYSTEM,
                    actor_id="task-graph",
                    metadata={
                        "phase": WorkflowPhase.IMPLEMENTATION.value,
                        "execution_id": execution_id,
                        "artifact_id": artifact.artifact_id,
                        "kind": ArtifactKind.IMPLEMENTATION_SUMMARY.value,
                        "version": artifact.version,
                    },
                )
            )
        except Exception:
            pass  # a documentation artifact must never crash the gate
        if readiness.eligible_for_auto_progression:
            _transition_phase(
                storage,
                task.id,
                WorkflowPhase.IMPLEMENTATION,
                WorkflowPhase.REVIEW,
                execution_id,
            )
            return {"workflow_phase": WorkflowPhase.REVIEW.value, "gate_eligible": True}
        storage.append_event(
            Event(
                task_id=task.id,
                event_type=EventType.WORKFLOW_PHASE_WAITING_FOR_HUMAN,
                actor_type=ActorType.SYSTEM,
                actor_id="task-graph",
                metadata={
                    "phase": WorkflowPhase.IMPLEMENTATION.value,
                    "execution_id": execution_id,
                    "score": readiness.score,
                    "blocking_failures": readiness.blocking_failures,
                    "blocking_needs_human": readiness.blocking_needs_human,
                },
            )
        )
        return {"workflow_phase": WorkflowPhase.IMPLEMENTATION.value, "gate_eligible": False}

    # ---- terminals ----------------------------------------------------------

    def waiting_for_human(_state: TaskGraphState) -> TaskGraphState:
        _change_status(storage, task.id, TaskStatus.WAITING_FOR_HUMAN, "task-graph", execution_id)
        return {"status": TaskStatus.WAITING_FOR_HUMAN.value}

    def paused(_state: TaskGraphState) -> TaskGraphState:
        _change_status(storage, task.id, TaskStatus.PAUSED_BY_HUMAN, "task-graph", execution_id)
        return {"status": TaskStatus.PAUSED_BY_HUMAN.value}

    def failed(state: TaskGraphState) -> TaskGraphState:
        _change_status(storage, task.id, TaskStatus.FAILED, "task-graph", execution_id)
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

    # ---- routing --------------------------------------------------------------

    def after_load(
        state: TaskGraphState,
    ) -> Literal[
        "prepare_workspace", "brainstorm", "plan", "select_model_implementation",
        "review", "waiting_for_human", "failed",
    ]:
        if state.get("fatal_error"):
            return "failed"
        if not state.get("continuation"):
            return "prepare_workspace"
        return _ENTRY_NODE_BY_PHASE[WorkflowPhase(state["workflow_phase"])]

    def after_prepare(
        state: TaskGraphState,
    ) -> Literal["brainstorm", "plan", "select_model_implementation", "review", "waiting_for_human",
                 "failed"]:
        if state.get("fatal_error"):
            return "failed"
        return _ENTRY_NODE_BY_PHASE[WorkflowPhase(state["workflow_phase"])]

    def after_brainstorm(state: TaskGraphState) -> Literal["plan", "waiting_for_human", "failed"]:
        if state.get("fatal_error"):
            return "failed"
        return "plan" if state.get("gate_eligible") else "waiting_for_human"

    def after_plan(
        state: TaskGraphState,
    ) -> Literal["select_model_implementation", "waiting_for_human", "failed"]:
        if state.get("fatal_error"):
            return "failed"
        return "select_model_implementation" if state.get("gate_eligible") else "waiting_for_human"

    def after_review(state: TaskGraphState) -> Literal["waiting_for_human", "failed"]:
        if state.get("fatal_error"):
            return "failed"
        return "waiting_for_human"

    def after_implementation(state: TaskGraphState) -> Literal["verify", "paused", "failed"]:
        if state.get("execution_cancelled") or storage.is_pause_requested(task.id):
            return "paused"
        return "failed" if state.get("fatal_error") else "verify"

    def after_verification(
        state: TaskGraphState,
    ) -> Literal["implementation_gate", "analyze_implement", "escalate_model", "paused"]:
        if storage.is_pause_requested(task.id):
            return "paused"
        if state.get("verification_passed"):
            return "implementation_gate"
        if state.get("tier_attempt", 0) < max_attempts_per_tier:
            return "analyze_implement"
        if (
            router.escalate_selection(_selection_from_state(state), state.get("tier_attempt", 0))
            is not None
        ):
            return "escalate_model"
        # Bounded retries and escalation are exhausted: this is a gate stop
        # (BUSINESS/GATE STOP), not a system failure. The gate records the
        # failing deterministic-verification evidence and the task waits.
        return "implementation_gate"

    def after_implementation_gate(state: TaskGraphState) -> Literal["review", "waiting_for_human"]:
        return "review" if state.get("gate_eligible") else "waiting_for_human"

    builder = StateGraph(TaskGraphState)
    builder.add_node("load_task", load_task)
    builder.add_node("prepare_workspace", prepare_workspace)
    builder.add_node("brainstorm", brainstorm)
    builder.add_node("plan", plan)
    builder.add_node("select_model_implementation", select_model_implementation)
    builder.add_node("escalate_model", escalate_model)
    builder.add_node("analyze_implement", analyze_implement)
    builder.add_node("verify", verify)
    builder.add_node("implementation_gate", implementation_gate)
    builder.add_node("review", review)
    builder.add_node("waiting_for_human", waiting_for_human)
    builder.add_node("paused", paused)
    builder.add_node("failed", failed)
    builder.add_edge(START, "load_task")
    builder.add_conditional_edges("load_task", after_load)
    builder.add_conditional_edges("prepare_workspace", after_prepare)
    builder.add_conditional_edges("brainstorm", after_brainstorm)
    builder.add_conditional_edges("plan", after_plan)
    builder.add_edge("select_model_implementation", "analyze_implement")
    builder.add_conditional_edges("analyze_implement", after_implementation)
    builder.add_conditional_edges("verify", after_verification)
    builder.add_edge("escalate_model", "analyze_implement")
    builder.add_conditional_edges("implementation_gate", after_implementation_gate)
    builder.add_conditional_edges("review", after_review)
    builder.add_edge("waiting_for_human", END)
    builder.add_edge("paused", END)
    builder.add_edge("failed", END)
    return builder.compile(checkpointer=checkpointer)


# ---- shared helpers (module-level: no closure state needed) -----------------


def _execute_structured(
    executor: AgentExecutor, request: ExecutionRequest, kind: ArtifactKind
) -> tuple[ExecutionResult, dict[str, object] | None, list[AgentActivity]]:
    """Execute a read-only phase, with one bounded format-repair retry.

    Never loops more than once: a still-malformed second attempt is left to the
    caller, which turns it into deterministic ``NEEDS_HUMAN`` gate evidence
    rather than an unbounded "please fix your JSON" loop.
    """

    result = executor.execute(request)
    payload = _validate_or_none(kind, result.structured_output)
    activities = list(result.activities)
    if payload is None:
        repaired_request = request.model_copy(update={"format_repair_attempt": True})
        result = executor.execute(repaired_request)
        activities += result.activities
        payload = _validate_or_none(kind, result.structured_output)
    return result, payload, activities


def _validate_or_none(kind: ArtifactKind, structured_output: object) -> dict[str, object] | None:
    if not isinstance(structured_output, dict):
        return None
    try:
        return validate_artifact_payload(kind, structured_output)
    except Exception:
        return None


def _upstream_artifacts(
    storage: SQLiteStorage, task_id: str, kinds: frozenset[ArtifactKind]
) -> dict[str, dict[str, object]]:
    if not kinds:
        return {}
    current = {
        artifact.kind: artifact.payload
        for artifact in storage.list_workflow_artifacts(task_id, current_only=True)
    }
    return {kind.value: current[kind] for kind in kinds if kind in current}


def _canonical_verification_evidence(
    storage: SQLiteStorage, task_id: str
) -> dict[str, object]:
    """Return the latest persisted verifier event as bounded machine facts.

    Absence or malformed/incomplete verifier evidence fails closed. This helper
    never infers truth from an agent artifact or natural-language summary.
    """

    for event in reversed(storage.get_events(task_id)):
        if event.event_type not in {EventType.TEST_PASSED, EventType.TEST_FAILED}:
            continue
        metadata = event.metadata
        error = metadata.get("error")
        return {
            "status": "PASS" if event.event_type is EventType.TEST_PASSED else "FAIL",
            "event_sequence_id": event.sequence_id,
            "mode": metadata.get("verification_mode"),
            "command": [str(item) for item in metadata.get("command", [])],
            "task_specific_passed": metadata.get("task_specific_passed"),
            "task_specific_passed_tests": metadata.get("task_specific_passed_tests"),
            "broad_regression_passed": metadata.get("broad_regression_passed"),
            "known_baseline_failures": int(metadata.get("baseline_warning_count", 0)),
            "new_regressions": int(metadata.get("new_regression_count", 0)),
            "pre_existing_failures": [
                str(item) for item in metadata.get("pre_existing_failures", [])
            ],
            "new_failures": [str(item) for item in metadata.get("new_failures", [])],
            "infrastructure_error": str(error) if error else None,
        }
    return {
        "status": "FAIL",
        "event_sequence_id": None,
        "mode": None,
        "command": [],
        "task_specific_passed": None,
        "task_specific_passed_tests": None,
        "broad_regression_passed": None,
        "known_baseline_failures": 0,
        "new_regressions": 0,
        "pre_existing_failures": [],
        "new_failures": [],
        "infrastructure_error": "No persisted verification result is available.",
    }


def _canonical_verification_lines(evidence: dict[str, object]) -> list[str]:
    """Render concise factual lines for the user-facing artifact."""

    lines = [f"Platform verification: {evidence['status']}"]
    task_passed = evidence.get("task_specific_passed")
    task_count = evidence.get("task_specific_passed_tests")
    if task_passed is not None:
        detail = "PASS" if task_passed else "FAIL"
        if isinstance(task_count, int):
            detail += f" ({task_count} passed)"
        lines.append(f"Focused/task verification: {detail}")
    lines.append(
        f"Known baseline failures: {evidence.get('known_baseline_failures', 0)} unchanged"
    )
    lines.append(f"New regressions: {evidence.get('new_regressions', 0)}")
    if evidence.get("infrastructure_error"):
        lines.append(f"Verification infrastructure: {evidence['infrastructure_error']}")
    return lines


def _persist_gate(
    storage: SQLiteStorage,
    task_id: str,
    phase: WorkflowPhase,
    results: list[ChecklistResult],
    execution_id: str,
):
    items = resolve_checklist(phase, results)
    readiness = calculate_readiness(items)
    storage.create_checklist_evaluation(task_id, phase, items, readiness, "task-graph")
    storage.append_event(
        Event(
            task_id=task_id,
            event_type=EventType.WORKFLOW_PHASE_GATE_EVALUATED,
            actor_type=ActorType.SYSTEM,
            actor_id="task-graph",
            metadata={
                "phase": phase.value,
                "execution_id": execution_id,
                "score": readiness.score,
                "blocking_failures": readiness.blocking_failures,
                "blocking_needs_human": readiness.blocking_needs_human,
                "eligible_for_auto_progression": readiness.eligible_for_auto_progression,
            },
        )
    )
    return readiness


def _transition_phase(
    storage: SQLiteStorage,
    task_id: str,
    expected_from: WorkflowPhase,
    target: WorkflowPhase,
    execution_id: str,
) -> None:
    event = Event(
        task_id=task_id,
        event_type=EventType.WORKFLOW_PHASE_CHANGED,
        actor_type=ActorType.SYSTEM,
        actor_id="workflow-gate",
        metadata={
            "execution_id": execution_id,
            "from_phase": expected_from.value,
            "to_phase": target.value,
            "transition_mode": "AUTOMATIC",
        },
    )
    storage.transition_workflow_phase(task_id, expected_from, target, event)


def _tool_calls(activities: list[AgentActivity]) -> list[str]:
    return [
        str(activity.metadata.get("tool"))
        for activity in activities
        if activity.activity_type is AgentActivityType.TOOL_CALL
    ]


def _checklist_results(
    phase: WorkflowPhase,
    payload: dict[str, object] | None,
    activities: list[AgentActivity],
    violation: bool,
) -> list[ChecklistResult]:
    if phase is WorkflowPhase.BRAINSTORM:
        return _brainstorm_checklist(payload, activities, violation)
    if phase is WorkflowPhase.PLAN:
        return _plan_checklist(payload, violation)
    return _review_checklist(payload, violation)


def _violation_results(phase: WorkflowPhase) -> list[ChecklistResult]:
    evidence = "Unexpected workspace mutation detected during a read-only phase"
    return [
        ChecklistResult(key=definition.key, status=ChecklistStatus.FAIL, evidence=evidence)
        for definition in CHECKLISTS[phase]
    ]


def _missing_payload_results(phase: WorkflowPhase, what: str) -> list[ChecklistResult]:
    evidence = f"Agent did not return a valid structured {what} after one bounded retry"
    return [
        ChecklistResult(key=definition.key, status=ChecklistStatus.NEEDS_HUMAN, evidence=evidence)
        for definition in CHECKLISTS[phase]
    ]


def _brainstorm_checklist(
    payload: dict[str, object] | None, activities: list[AgentActivity], violation: bool
) -> list[ChecklistResult]:
    if violation:
        return _violation_results(WorkflowPhase.BRAINSTORM)
    if payload is None:
        return _missing_payload_results(WorkflowPhase.BRAINSTORM, "Brainstorm Summary")
    inspected = any(tool in _READ_ONLY_INSPECTION_TOOLS for tool in _tool_calls(activities))
    options = payload.get("options") or []
    questions = payload.get("questions") or []
    assumptions = payload.get("assumptions") or []
    return [
        ChecklistResult(
            key="requirements_understood",
            status=ChecklistStatus.PASS if payload.get("summary") else ChecklistStatus.FAIL,
            evidence=f"Summary provided: {bool(payload.get('summary'))}",
        ),
        ChecklistResult(
            key="repository_context_inspected",
            status=ChecklistStatus.PASS if inspected else ChecklistStatus.FAIL,
            evidence=(
                "Observed Read/Glob/Grep tool use"
                if inspected
                else "No repository-inspection tool calls were observed"
            ),
        ),
        ChecklistResult(
            key="viable_direction_identified",
            status=ChecklistStatus.PASS if options else ChecklistStatus.FAIL,
            evidence=f"{len(options)} option(s) identified",
        ),
        ChecklistResult(
            key="blocking_questions_resolved",
            status=ChecklistStatus.NEEDS_HUMAN if questions else ChecklistStatus.PASS,
            evidence=(
                f"{len(questions)} unresolved blocking question(s)"
                if questions
                else "No blocking questions were raised"
            ),
        ),
        ChecklistResult(
            key="assumptions_documented",
            status=ChecklistStatus.PASS if assumptions else ChecklistStatus.FAIL,
            evidence=f"{len(assumptions)} assumption(s) documented",
        ),
    ]


def _plan_checklist(payload: dict[str, object] | None, violation: bool) -> list[ChecklistResult]:
    if violation:
        return _violation_results(WorkflowPhase.PLAN)
    if payload is None:
        return _missing_payload_results(WorkflowPhase.PLAN, "Plan")
    steps = payload.get("steps") or []
    files = payload.get("files") or []
    tests = payload.get("tests") or []
    open_questions = payload.get("open_questions") or []
    return [
        ChecklistResult(
            key="implementation_steps_complete",
            status=ChecklistStatus.PASS if steps else ChecklistStatus.FAIL,
            evidence=f"{len(steps)} implementation step(s) defined",
        ),
        ChecklistResult(
            key="affected_files_identified",
            status=ChecklistStatus.PASS if files else ChecklistStatus.FAIL,
            evidence=f"{len(files)} affected file(s) identified",
        ),
        ChecklistResult(
            key="validation_plan_defined",
            status=ChecklistStatus.PASS if tests else ChecklistStatus.FAIL,
            evidence=f"{len(tests)} validation step(s) defined",
        ),
        ChecklistResult(
            key="risks_addressed",
            status=ChecklistStatus.PASS,
            evidence="Risk list is structurally present (empty means none identified)",
        ),
        ChecklistResult(
            key="open_questions_resolved",
            status=ChecklistStatus.NEEDS_HUMAN if open_questions else ChecklistStatus.PASS,
            evidence=(
                f"{len(open_questions)} open question(s) block implementation"
                if open_questions
                else "No open questions block implementation"
            ),
        ),
        ChecklistResult(
            key="plan_clarity", status=ChecklistStatus.PASS, evidence="Plan summary is present"
        ),
    ]


def _review_checklist(payload: dict[str, object] | None, violation: bool) -> list[ChecklistResult]:
    if violation:
        return _violation_results(WorkflowPhase.REVIEW)
    if payload is None:
        return _missing_payload_results(WorkflowPhase.REVIEW, "Review Report")
    critical = payload.get("critical_findings") or []
    major = payload.get("major_findings") or []
    minor = payload.get("minor_findings") or []
    return [
        ChecklistResult(
            key="no_critical_findings",
            status=ChecklistStatus.PASS if not critical else ChecklistStatus.FAIL,
            evidence=f"{len(critical)} critical finding(s)",
        ),
        ChecklistResult(
            key="no_major_blocking_findings",
            status=ChecklistStatus.PASS if not major else ChecklistStatus.FAIL,
            evidence=f"{len(major)} major finding(s)",
        ),
        ChecklistResult(
            key="requirements_satisfied",
            status=ChecklistStatus.PASS if not critical and not major else ChecklistStatus.FAIL,
            evidence=str(payload.get("requirements_assessment", ""))[:500] or "No assessment",
        ),
        ChecklistResult(
            key="tests_sufficient",
            status=ChecklistStatus.PASS if payload.get("test_assessment") else ChecklistStatus.FAIL,
            evidence=str(payload.get("test_assessment", ""))[:500] or "No assessment",
        ),
        ChecklistResult(
            key="no_minor_findings",
            status=ChecklistStatus.PASS if not minor else ChecklistStatus.FAIL,
            evidence=f"{len(minor)} minor finding(s)",
        ),
    ]


def _implementation_checklist(
    files_changed: list[str],
    verification_passed: bool,
    known_issues: list[str],
    structured_valid: bool,
) -> list[ChecklistResult]:
    return [
        ChecklistResult(
            key="required_changes_present",
            status=ChecklistStatus.PASS if files_changed else ChecklistStatus.FAIL,
            evidence=f"{len(files_changed)} file(s) changed in the workspace",
        ),
        ChecklistResult(
            key="deterministic_verification_passed",
            status=ChecklistStatus.PASS if verification_passed else ChecklistStatus.FAIL,
            evidence=(
                "Deterministic verification passed"
                if verification_passed
                else "Deterministic verification failed or did not run"
            ),
        ),
        ChecklistResult(
            key="implementation_matches_plan",
            status=(
                ChecklistStatus.PASS
                if files_changed and verification_passed
                else ChecklistStatus.FAIL
            ),
            evidence="Workspace changes accompany passing verification",
        ),
        ChecklistResult(
            key="no_known_blocking_issues",
            status=(
                ChecklistStatus.NEEDS_HUMAN
                if structured_valid and known_issues
                else ChecklistStatus.PASS
            ),
            evidence=(
                f"{len(known_issues)} known issue(s) reported" if known_issues else "None reported"
            ),
        ),
        ChecklistResult(
            key="cleanup_quality",
            status=ChecklistStatus.PASS if structured_valid else ChecklistStatus.FAIL,
            evidence=(
                "Structured implementation summary supplied" if structured_valid else "Missing"
            ),
        ),
    ]


def _selection_from_state(state: TaskGraphState) -> ModelSelection:
    return ModelSelection(
        tier=state["selected_tier"],
        model=state["selected_model"],
        reason=state["selection_reason"],
        requested_selection=state["requested_selection"],
        effective_selection=state["effective_selection"],
        provider=state["model_provider"],
        resolution_source=state["resolution_source"],
        workflow_phase=WorkflowPhase.IMPLEMENTATION,
    )


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
