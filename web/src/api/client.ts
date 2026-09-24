import type {
  ConfigResponse,
  CreateTaskRequest,
  CreateTaskResponse,
  ControlRequest,
  ControlResponse,
  ConversationMessage,
  CurrentUser,
  DiffResponse,
  PlatformEvent,
  PostMessageRequest,
  PostMessageResponse,
  PresenceResponse,
  RepositoryOption,
  AssignableUser,
  TaskDetail,
  TaskListItem,
  LogicalModel,
  ModelCatalogEntry,
  TaskModelRouting,
} from "../types/api";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

let onUnauthorized: () => void = () => undefined;

/** Called whenever the server says the session is gone (401): return to login. */
export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

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
  if (status === 401) return detail ?? "Your session has ended. Please sign in again.";
  if (status === 403) return detail ?? "You do not have permission to do that.";
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
  if (response.status === 401 && !path.startsWith("/api/auth/")) onUnauthorized();
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: unknown } | null;
    const detail = typeof payload?.detail === "string" ? payload.detail : undefined;
    throw new ApiError(friendlyMessage(response.status, detail), response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

const post = (body?: unknown): RequestInit => ({
  method: "POST",
  headers: body === undefined ? undefined : { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

const put = (body: unknown): RequestInit => ({
  method: "PUT",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const task = (taskId: string) => `/api/tasks/${encodeURIComponent(taskId)}`;

export const api = {
  login: (username: string, token: string) =>
    request<CurrentUser>("/api/auth/login", post({ username, token })),
  me: (signal?: AbortSignal) => request<CurrentUser>("/api/auth/me", { signal }),
  logout: () => request<void>("/api/auth/logout", post()),
  heartbeat: (taskId: string) => request<PresenceResponse>(`${task(taskId)}/presence`, post()),
  getConfig: (signal?: AbortSignal) => request<ConfigResponse>("/api/config", { signal }),
  listTasks: (signal?: AbortSignal) => request<TaskListItem[]>("/api/tasks", { signal }),
  listRepositories: (signal?: AbortSignal) =>
    request<RepositoryOption[]>("/api/repositories", { signal }),
  listAssignableUsers: (signal?: AbortSignal) =>
    request<AssignableUser[]>("/api/users/assignable", { signal }),
  createTask: (body: CreateTaskRequest) =>
    request<CreateTaskResponse>("/api/tasks", post(body)),
  getTask: (taskId: string, signal?: AbortSignal) => request<TaskDetail>(task(taskId), { signal }),
  listModels: (signal?: AbortSignal) => request<ModelCatalogEntry[]>("/api/models", { signal }),
  getModelRouting: (taskId: string, signal?: AbortSignal) =>
    request<TaskModelRouting>(`${task(taskId)}/model-routing`, { signal }),
  setDefaultModel: (taskId: string, selection: LogicalModel) =>
    request(`${task(taskId)}/model-routing/default`, put({ selection })),
  setPhaseModel: (taskId: string, phase: string, selection: LogicalModel) =>
    request(`${task(taskId)}/model-routing/phases/${phase}`, put({ selection })),
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
