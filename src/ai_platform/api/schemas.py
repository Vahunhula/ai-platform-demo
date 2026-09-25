"""Intentional public contracts for the HTTP API (mirrored in web/src/types/api.ts)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_platform.models import LogicalModel, ModelResolutionSource
from ai_platform.sessions import MAX_MESSAGE_LENGTH
from ai_platform.workflow import ArtifactKind, ChecklistResult, WorkflowPhase


class HealthResponse(BaseModel):
    status: str = "ok"


class ConfigResponse(BaseModel):
    """Server capabilities relevant to the browser."""

    runner_enabled: bool
    max_message_length: int
    presence_heartbeat_seconds: int


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    token: str = Field(min_length=1, max_length=200)


class UserResponse(BaseModel):
    """Public identity of a user: never tokens, hashes or session data."""

    id: str
    username: str
    display_name: str
    role: Literal["viewer", "developer", "admin"]
    can_modify_tasks: bool


class AssignableUserResponse(BaseModel):
    id: str
    username: str
    display_name: str


class RepositoryResponse(BaseModel):
    id: str
    slug: str
    display_name: str
    default_branch: str
    enabled: bool


class CreateTaskRequest(BaseModel):
    """A browser request contains registry/user IDs, never actors or paths."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10_000)
    repository_id: str = Field(min_length=1, max_length=80)
    base_branch: str = Field(min_length=1, max_length=200)
    assignee_user_id: str = Field(min_length=1, max_length=80)
    jira_key: str | None = Field(default=None, max_length=80)

    @field_validator("title", "description", "repository_id", "base_branch", "assignee_user_id")
    @classmethod
    def required_trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Field must not be blank")
        return value

    @field_validator("jira_key")
    @classmethod
    def optional_trimmed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class CreateTaskResponse(BaseModel):
    id: str
    title: str
    description: str
    repository_id: str
    base_branch: str
    assignee_user_id: str
    jira_key: str | None
    status: Literal["READY"]
    workflow_phase: Literal["BRAINSTORM"]
    created_by: str
    created_at: datetime
    workspace_ready: Literal[True] = True


class PresenceUser(BaseModel):
    user_id: str
    username: str
    display_name: str


class PresenceResponse(BaseModel):
    task_id: str
    viewers: list[PresenceUser]


class TaskListItem(BaseModel):
    id: str
    title: str
    difficulty: str
    status: str
    workflow_phase: WorkflowPhase
    model_tier: str | None
    writer: str | None
    updated_at: datetime


class VerificationResultResponse(BaseModel):
    status: str
    sequence_id: int
    timestamp: datetime
    exit_code: int | None = None
    duration_seconds: float | None = None
    timed_out: bool | None = None
    stdout: str | None = None
    stderr: str | None = None
    error: str | None = None
    verification_mode: str | None = None
    task_specific_targets: list[str] = Field(default_factory=list)
    task_specific_passed: bool | None = None
    task_specific_passed_tests: int | None = None
    broad_regression_passed: bool | None = None
    baseline_warning_count: int = 0
    new_regression_count: int = 0
    pre_existing_failures: list[str] = Field(default_factory=list)
    fixed_failures: list[str] = Field(default_factory=list)
    new_failures: list[str] = Field(default_factory=list)


class MessagingState(BaseModel):
    """Whether the browser may send a message to this task now, and why not."""

    accepting: bool
    reason: str | None


class ActionState(BaseModel):
    """Whether a lifecycle action is currently allowed, decided by the backend."""

    allowed: bool
    reason: str | None


class TaskActions(BaseModel):
    start: ActionState
    pause: ActionState
    resume: ActionState
    approve: ActionState
    defer: ActionState
    reject: ActionState
    reset: ActionState


class ReadinessSummary(BaseModel):
    """The most recent readiness gate evaluated for this task, of any phase."""

    phase: WorkflowPhase
    score: float
    eligible_for_auto_progression: bool


class TaskDetailResponse(TaskListItem):
    description: str
    acceptance_criteria: list[str]
    model_name: str | None
    workspace_id: str | None
    current_attempt: int
    verification_status: str
    verification_result: VerificationResultResponse | None
    agent_working: bool
    pause_requested: bool
    queued_messages: int
    messaging: MessagingState
    actions: TaskActions
    created_at: datetime
    default_model_selection: LogicalModel
    latest_readiness: ReadinessSummary | None = None
    disposition: Literal["CONFIRMED", "DEFERRED"] | None
    can_remove: bool
    remove_disabled_reason: str | None
    # Safe workspace identity for the Workspace tab; never an absolute host path.
    repository_id: str | None = None
    base_branch: str | None = None


class ModelCatalogResponse(BaseModel):
    logical_id: LogicalModel
    display_name: str
    provider: str | None
    enabled: bool


class ModelPreferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection: LogicalModel


class ResolvedModelResponse(BaseModel):
    requested_selection: LogicalModel
    effective_selection: LogicalModel
    provider: str
    concrete_model_id: str
    source: ModelResolutionSource


class PhaseModelRoutingResponse(BaseModel):
    phase: WorkflowPhase
    selection: LogicalModel
    resolved: ResolvedModelResponse | None = None
    error: str | None = None


class TaskModelRoutingResponse(BaseModel):
    task_id: str
    default_model_selection: LogicalModel
    phases: list[PhaseModelRoutingResponse]


class ModelPreferenceUpdateResponse(BaseModel):
    task_id: str
    selection: LogicalModel
    phase: WorkflowPhase | None = None
    changed: bool
    applies_to_next_turn: Literal[True] = True


class EventResponse(BaseModel):
    sequence_id: int
    timestamp: datetime
    event_type: str
    actor_type: str
    actor_id: str
    # Display name recorded with the event (historical), falling back to actor_id.
    actor_display_name: str
    execution_id: str | None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DiffResponse(BaseModel):
    task_id: str
    diff: str


class PhaseTransitionRequest(BaseModel):
    """Optimistic manual transition; actor and mode are server-owned."""

    model_config = ConfigDict(extra="forbid")

    from_phase: WorkflowPhase
    to_phase: WorkflowPhase
    reason: str | None = Field(default=None, max_length=4000)


class PhaseTransitionResponse(BaseModel):
    task_id: str
    from_phase: WorkflowPhase
    workflow_phase: WorkflowPhase
    transition_mode: Literal["MANUAL"] = "MANUAL"


class CreateArtifactRequest(BaseModel):
    """Artifact producer identity is always resolved from the session."""

    model_config = ConfigDict(extra="forbid")

    phase: WorkflowPhase
    kind: ArtifactKind
    payload: dict[str, Any]


class ArtifactResponse(BaseModel):
    artifact_id: str
    task_id: str
    phase: WorkflowPhase
    kind: ArtifactKind
    version: int
    payload: dict[str, Any]
    created_by: str
    created_at: datetime
    supersedes_artifact_id: str | None


class CreateChecklistRequest(BaseModel):
    """Only results/evidence are submitted; weights and blockers are platform-owned."""

    model_config = ConfigDict(extra="forbid")

    phase: WorkflowPhase
    items: list[ChecklistResult]


class ChecklistItemResponse(BaseModel):
    key: str
    label: str
    weight: int
    blocking: bool
    status: str
    evidence: str


class ReadinessResponse(BaseModel):
    score: float
    blocking_failures: list[str]
    blocking_needs_human: list[str]
    eligible_for_auto_progression: bool


class ChecklistEvaluationResponse(BaseModel):
    evaluation_id: str
    task_id: str
    phase: WorkflowPhase
    evaluation_number: int
    created_by: str
    created_at: datetime
    items: list[ChecklistItemResponse]
    readiness: ReadinessResponse


MessageStatusName = Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED"]

ChatItemTypeName = Literal[
    "human_message",
    "agent_message",
    "phase_result",
    "human_input_required",
    "platform_activity",
    "command_result",
]


class BlockingCheckResponse(BaseModel):
    """One blocking/needs-human checklist item explaining why a gate did not pass."""

    key: str
    label: str
    status: str
    evidence: str


class MessageResponse(BaseModel):
    """One public Chat-timeline item.

    Chat is a projection over durable domain data: besides plain human/agent
    conversation messages, this also carries phase output, waiting-for-human
    questions, phase transitions, and command results, so the browser never
    has to infer any of that from raw Activity events. Only the fields
    relevant to ``type`` are populated; the rest are ``None``.
    """

    id: str
    task_id: str
    type: ChatItemTypeName
    role: Literal["human", "agent", "platform"]
    actor_id: str
    actor_display_name: str
    # A short header, e.g. "Claude · Plan" or "Needs your input · Plan".
    title: str | None = None
    content: str
    timestamp: datetime
    sequence_id: int
    turn_id: str | None
    workflow_phase: WorkflowPhase | None = None
    artifact_kind: ArtifactKind | None = None
    artifact_version: int | None = None
    readiness_score: float | None = None
    requires_human_input: bool = False
    blocking_checks: list[BlockingCheckResponse] | None = None
    logical_model: str | None = None
    concrete_model: str | None = None
    # Delivery state; only browser-submitted human messages have one.
    status: MessageStatusName | None
    error: str | None
    client_message_id: str | None
    channel: str | None


class PostMessageRequest(BaseModel):
    """Browser message submission. The actor is resolved server-side, never sent."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(max_length=MAX_MESSAGE_LENGTH)
    client_message_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")

    @field_validator("message")
    @classmethod
    def message_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Message must not be empty")
        return stripped


class PostMessageResponse(BaseModel):
    status: Literal["accepted"] = "accepted"
    message_id: str
    client_message_id: str
    task_id: str
    message_status: MessageStatusName
    duplicate: bool


class CommandMetadataResponse(BaseModel):
    name: str
    description: str
    usage: str
    arguments: list[str]
    required_permission: Literal["viewer", "developer"]
    available: bool
    disabled_reason: str | None
    mutating: bool


class ExecuteCommandRequest(BaseModel):
    """Command text only; actor identity always comes from the session."""

    model_config = ConfigDict(extra="forbid")

    command_text: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)
    client_command_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")


class CommandResultResponse(BaseModel):
    command: str
    status: Literal["completed", "accepted"]
    message: str
    data: dict[str, Any]


class ClaudeCommandMetadataResponse(BaseModel):
    namespace: Literal["claude"]
    command: str
    description: str
    usage: str
    classification: Literal["NATIVE", "ADAPTED", "DISABLED", "FUTURE_INFRA_ONLY"]
    required_permission: Literal["viewer", "developer"]
    executor_capability: str
    available: bool
    disabled_reason: str | None


ClientActionId = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")


def _optional_message(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_action_id: str = ClientActionId


class ResumeRequest(BaseModel):
    """Optional instruction, like ``ai-platform resume TASK --message``."""

    model_config = ConfigDict(extra="forbid")

    client_action_id: str = ClientActionId
    message: str | None = Field(default=None, max_length=MAX_MESSAGE_LENGTH)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str | None) -> str | None:
        return _optional_message(value)


class RejectRequest(BaseModel):
    """Required review feedback, like ``ai-platform reject TASK MESSAGE``."""

    model_config = ConfigDict(extra="forbid")

    client_action_id: str = ClientActionId
    message: str = Field(max_length=MAX_MESSAGE_LENGTH)

    @field_validator("message")
    @classmethod
    def message_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Rejection feedback must not be empty")
        return value.strip()


class ResetRequest(BaseModel):
    """Explicit confirmation, the HTTP equivalent of answering the CLI's prompt."""

    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True]


ActionName = Literal["start", "pause", "resume", "approve", "defer", "reject", "reset"]


class ControlResponse(BaseModel):
    """``accepted``: an agent turn was launched in the background (202).
    ``completed``: the state change is already done (200)."""

    status: Literal["accepted", "completed"]
    action: ActionName
    task_id: str
    task_status: str
    execution_id: str | None = None
    client_action_id: str | None = None
    duplicate: bool = False
    deferred: bool | None = None
