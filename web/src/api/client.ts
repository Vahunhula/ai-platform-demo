import type {
  ConfigResponse,
  ControlRequest,
  ControlResponse,
  ConversationMessage,
  DiffResponse,
  PlatformEvent,
  PostMessageRequest,
  PostMessageResponse,
  TaskDetail,
  TaskListItem,
} from "../types/api";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

/** An HTTP error with a user-presentable message; `status` is 0 for network failures. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }

  /** Safe to resend with the same client_message_id (idempotent on the server). */
  get retryable(): boolean {
    return this.status === 0 || this.status >= 500;
  }
}

function friendlyMessage(status: number, detail: string | undefined): string {
  if (status >= 500 && status !== 503) return "The server hit an unexpected error. Try again.";
  if (status === 422) return detail && !detail.startsWith("[") ? detail : "The request was invalid.";
  return detail ?? `Request failed (${status}).`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, init);
  } catch (reason) {
    if (reason instanceof DOMException && reason.name === "AbortError") throw reason;
    throw new ApiError("Could not reach the server. Check the connection and retry.", 0);
  }
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: unknown } | null;
    const detail = typeof payload?.detail === "string" ? payload.detail : undefined;
    throw new ApiError(friendlyMessage(response.status, detail), response.status);
  }
  return response.json() as Promise<T>;
}

const task = (taskId: string) => `/api/tasks/${encodeURIComponent(taskId)}`;

export const api = {
  getConfig: (signal?: AbortSignal) => request<ConfigResponse>("/api/config", { signal }),
  listTasks: (signal?: AbortSignal) => request<TaskListItem[]>("/api/tasks", { signal }),
  getTask: (taskId: string, signal?: AbortSignal) => request<TaskDetail>(task(taskId), { signal }),
  getEvents: (taskId: string, signal?: AbortSignal) =>
    request<PlatformEvent[]>(`${task(taskId)}/events`, { signal }),
  getDiff: (taskId: string, signal?: AbortSignal) =>
    request<DiffResponse>(`${task(taskId)}/diff`, { signal }),
  getMessages: (taskId: string, signal?: AbortSignal) =>
    request<ConversationMessage[]>(`${task(taskId)}/messages`, { signal }),
  postMessage: (taskId: string, body: PostMessageRequest) =>
    request<PostMessageResponse>(`${task(taskId)}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  control: (taskId: string, { action, ...body }: ControlRequest) =>
    request<ControlResponse>(`${task(taskId)}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: Object.keys(body).length ? JSON.stringify(body) : undefined,
    }),
  streamUrl: (taskId: string, afterSequence: number) =>
    `${API_BASE}${task(taskId)}/stream?after=${afterSequence}`,
};
