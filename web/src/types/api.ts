// Mirrors the Pydantic contracts in src/ai_platform/api/schemas.py. Keep both in sync.

export interface ConfigResponse {
  messaging_enabled: boolean;
  web_actor: string | null;
  runner_enabled: boolean;
  max_message_length: number;
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

export interface TaskDetail extends TaskListItem {
  description: string;
  acceptance_criteria: string[];
  model_name: string | null;
  workspace_id: string | null;
  current_attempt: number;
  verification_status: string;
  verification_result: VerificationResult | null;
  agent_working: boolean;
  queued_messages: number;
  messaging: MessagingState;
  created_at: string;
}

export interface PlatformEvent {
  sequence_id: number;
  timestamp: string;
  event_type: string;
  actor_type: string;
  actor_id: string;
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
