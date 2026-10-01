// Mirrors the Pydantic contracts in src/ai_platform/api/schemas.py. Keep both in sync.

export interface ConfigResponse {
  runner_enabled: boolean;
  max_message_length: number;
  presence_heartbeat_seconds: number;
}

export type Role = "viewer" | "developer" | "admin";

/** The authenticated user (from the session cookie; never stored by the browser). */
export interface CurrentUser {
  id: string;
  username: string;
  display_name: string;
  role: Role;
  can_modify_tasks: boolean;
}

export interface PresenceUser {
  user_id: string;
  username: string;
  display_name: string;
}

export interface PresenceResponse {
  task_id: string;
  viewers: PresenceUser[];
}

export interface RepositoryOption {
  id: string;
  slug: string;
  display_name: string;
  default_branch: string;
  enabled: boolean;
}

export interface AssignableUser {
  id: string;
  username: string;
  display_name: string;
}

export interface CreateTaskRequest {
  title: string;
  description: string;
  repository_id: string;
  base_branch: string;
  assignee_user_id: string;
  jira_key: string | null;
  acceptance_test_stories: string | null;
  uploaded_test_files: UploadedTestInput[];
}

export interface UploadedTestInput {
  filename: string;
  content: string;
}

export interface CreateTaskResponse
  extends Omit<CreateTaskRequest, "acceptance_test_stories" | "uploaded_test_files"> {
  id: string;
  status: "READY";
  workflow_phase: "BRAINSTORM";
  created_by: string;
  created_at: string;
  workspace_ready: true;
}

export interface TaskListItem {
  id: string;
  title: string;
  difficulty: string;
  status: string;
  workflow_phase: "BRAINSTORM" | "PLAN" | "IMPLEMENTATION" | "REVIEW" | "HUMAN_REVIEW";
  model_tier: string | null;
  writer: string | null;
  disposition: "CONFIRMED" | "DEFERRED" | null;
  updated_at: string;
}

export interface VerificationResult {
  status: string;
  sequence_id: number;
  timestamp: string;
  exit_code: number | null;
  duration_seconds: number | null;
  timed_out: boolean | null;
  stdout: string | null;
  stderr: string | null;
  error: string | null;
  verification_mode: string | null;
  task_specific_targets: string[];
  task_specific_passed: boolean | null;
  task_specific_passed_tests: number | null;
  task_acceptance_status: "PASS" | "FAIL" | "NOT_PROVIDED";
  task_acceptance_targets: string[];
  broad_regression_passed: boolean | null;
  baseline_warning_count: number;
  new_regression_count: number;
  pre_existing_failures: string[];
  fixed_failures: string[];
  new_failures: string[];
}

export interface TaskTestFile {
  filename: string;
  relative_path: string;
  source: "GENERATED" | "UPLOADED";
  content: string;
  created_by: string;
  created_at: string;
  requirement_mapping: Record<string, string[]>;
}

export interface TaskTests {
  task_id: string;
  human_requirements: string | null;
  requirements_created_by: string | null;
  generation_status: "NOT_REQUESTED" | "PENDING" | "GENERATED" | "NEEDS_HUMAN";
  generation_message: string | null;
  generated_tests: TaskTestFile[];
  uploaded_tests: TaskTestFile[];
  repository_tests: string[];
  latest_verification: VerificationResult | null;
}

export interface MessagingState {
  accepting: boolean;
  reason: string | null;
}

export type ControlAction = "start" | "pause" | "resume" | "approve" | "defer" | "reject" | "reset";

/** Decided by the backend; the UI only renders it. */
export interface ActionState {
  allowed: boolean;
  reason: string | null;
}

export type TaskActions = Record<ControlAction, ActionState>;

export interface TaskDetail extends TaskListItem {
  description: string;
  acceptance_criteria: string[];
  model_name: string | null;
  workspace_id: string | null;
  current_attempt: number;
  verification_status: string;
  verification_result: VerificationResult | null;
  agent_working: boolean;
  pause_requested: boolean;
  queued_messages: number;
  messaging: MessagingState;
  actions: TaskActions;
  created_at: string;
  default_model_selection: LogicalModel;
  latest_readiness: ReadinessSummary | null;
  can_remove: boolean;
  remove_disabled_reason: string | null;
  repository_id: string | null;
  base_branch: string | null;
  assignee_display_name?: string | null;
}

export interface ReadinessSummary {
  phase: "BRAINSTORM" | "PLAN" | "IMPLEMENTATION" | "REVIEW" | "HUMAN_REVIEW";
  score: number;
  eligible_for_auto_progression: boolean;
}

export type LogicalModel = "AUTO" | "CLAUDE_SONNET" | "CLAUDE_OPUS";
export type AgentWorkflowPhase = "BRAINSTORM" | "PLAN" | "IMPLEMENTATION" | "REVIEW";

export interface ModelCatalogEntry {
  logical_id: LogicalModel;
  display_name: string;
  provider: string | null;
  enabled: boolean;
}

export interface ModelResolution {
  requested_selection: LogicalModel;
  effective_selection: LogicalModel;
  provider: string;
  concrete_model_id: string;
  source: "PHASE_OVERRIDE" | "TASK_DEFAULT" | "AUTO_POLICY";
}

export interface PhaseModelRouting {
  phase: AgentWorkflowPhase;
  selection: LogicalModel;
  resolved: ModelResolution | null;
  error: string | null;
}

export interface TaskModelRouting {
  task_id: string;
  default_model_selection: LogicalModel;
  phases: PhaseModelRouting[];
}

export interface PlatformEvent {
  sequence_id: number;
  timestamp: string;
  event_type: string;
  actor_type: string;
  actor_id: string;
  actor_display_name: string;
  execution_id: string | null;
  metadata: Record<string, unknown>;
}

export interface EventPage {
  items: PlatformEvent[];
  order: "asc" | "desc";
  limit: number;
  has_more: boolean;
  next_before_sequence: number | null;
  next_after_sequence: number | null;
}

export interface DiffResponse {
  task_id: string;
  diff: string;
}

export interface ChecklistItemResult {
  key: string;
  label: string;
  weight: number;
  blocking: boolean;
  status: "PASS" | "FAIL" | "NEEDS_HUMAN";
  evidence: string;
}

export interface ReadinessResult {
  score: number;
  blocking_failures: string[];
  blocking_needs_human: string[];
  eligible_for_auto_progression: boolean;
}

export interface ChecklistEvaluation {
  evaluation_id: string;
  task_id: string;
  phase: "BRAINSTORM" | "PLAN" | "IMPLEMENTATION" | "REVIEW" | "HUMAN_REVIEW";
  evaluation_number: number;
  created_by: string;
  created_at: string;
  items: ChecklistItemResult[];
  readiness: ReadinessResult;
}

export interface WorkflowArtifactResponse {
  artifact_id: string;
  task_id: string;
  phase: "BRAINSTORM" | "PLAN" | "IMPLEMENTATION" | "REVIEW" | "HUMAN_REVIEW";
  kind: string;
  version: number;
  payload: Record<string, unknown>;
  created_by: string;
  created_at: string;
  supersedes_artifact_id: string | null;
}

export type MessageStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";

export type ChatItemType =
  | "human_message"
  | "agent_message"
  | "phase_result"
  | "human_input_required"
  | "platform_activity"
  | "command_result";

export interface BlockingCheck {
  key: string;
  label: string;
  status: string;
  evidence: string;
}

/** One Chat-timeline item: a conversation message or a workflow projection. */
export interface ConversationMessage {
  id: string;
  task_id: string;
  type: ChatItemType;
  role: "human" | "agent" | "platform";
  actor_id: string;
  actor_display_name: string;
  title: string | null;
  content: string;
  timestamp: string;
  sequence_id: number;
  turn_id: string | null;
  workflow_phase: AgentWorkflowPhase | "HUMAN_REVIEW" | null;
  artifact_kind: string | null;
  artifact_version: number | null;
  readiness_score: number | null;
  requires_human_input: boolean;
  blocking_checks: BlockingCheck[] | null;
  logical_model: string | null;
  concrete_model: string | null;
  /** Delivery state; only browser-submitted human messages have one. */
  status: MessageStatus | null;
  error: string | null;
  client_message_id: string | null;
  channel: string | null;
}

/** The actor is resolved by the server; the browser never sends one. */
export interface PostMessageRequest {
  message: string;
  client_message_id: string;
}

export interface PostMessageResponse {
  status: "accepted";
  message_id: string;
  client_message_id: string;
  task_id: string;
  message_status: MessageStatus;
  duplicate: boolean;
}

export interface CommandMetadata {
  name: string;
  description: string;
  usage: string;
  arguments: string[];
  required_permission: "viewer" | "developer";
  available: boolean;
  disabled_reason: string | null;
  mutating: boolean;
}

export interface CommandResult {
  command: string;
  status: "completed" | "accepted";
  message: string;
  data: Record<string, unknown>;
}

export interface ClaudeCommandMetadata {
  namespace: "claude";
  command: string;
  description: string;
  usage: string;
  classification: "NATIVE" | "ADAPTED" | "DISABLED" | "FUTURE_INFRA_ONLY";
  required_permission: "viewer" | "developer";
  executor_capability: string;
  available: boolean;
  disabled_reason: string | null;
}

/** Bodies accepted by the control endpoints; never an actor, command or path. */
export type ControlRequest =
  | { action: "start"; client_action_id: string }
  | { action: "resume"; client_action_id: string; message?: string | null }
  | { action: "reject"; client_action_id: string; message: string }
  | { action: "pause" }
  | { action: "approve" }
  | { action: "defer" }
  | { action: "reset"; confirm: true };

export interface ControlResponse {
  /** "accepted": an agent turn was launched in the background. "completed": done. */
  status: "accepted" | "completed";
  action: ControlAction;
  task_id: string;
  task_status: string;
  execution_id: string | null;
  client_action_id: string | null;
  duplicate: boolean;
  deferred: boolean | null;
}
