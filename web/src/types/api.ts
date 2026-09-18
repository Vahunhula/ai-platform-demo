// Mirrors the Pydantic contracts in src/ai_platform/api/schemas.py. Keep both in sync.

export interface TaskListItem {
  id: string;
  title: string;
  difficulty: string;
  status: string;
  model_tier: string | null;
  writer: string | null;
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

export interface TaskDetail extends TaskListItem {
  description: string;
  acceptance_criteria: string[];
  model_name: string | null;
  workspace_id: string | null;
  current_attempt: number;
  verification_status: string;
  verification_result: VerificationResult | null;
  created_at: string;
  updated_at: string;
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
