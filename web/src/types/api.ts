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
}

export interface CreateTaskResponse extends CreateTaskRequest {
  id: string;
  status: "READY";
  created_by: string;
  created_at: string;
  workspace_ready: true;
}

export interface TaskListItem {
  id: string;
  title: string;
  difficulty: string;
  status: string;
  model_tier: string | null;
  writer: string | null;
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
}

export interface MessagingState {
  accepting: boolean;
  reason: string | null;
}

export type ControlAction = "start" | "pause" | "resume" | "approve" | "reject" | "reset";

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

export interface DiffResponse {
  task_id: string;
  diff: string;
}

export type MessageStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";

export interface ConversationMessage {
  id: string;
  task_id: string;
  role: "human" | "agent";
  actor_id: string;
  actor_display_name: string;
  content: string;
  timestamp: string;
  sequence_id: number;
  turn_id: string | null;
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

/** Bodies accepted by the control endpoints; never an actor, command or path. */
export type ControlRequest =
  | { action: "start"; client_action_id: string }
  | { action: "resume"; client_action_id: string; message?: string | null }
  | { action: "reject"; client_action_id: string; message: string }
  | { action: "pause" }
  | { action: "approve" }
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
