import type { DiffResponse, PlatformEvent, TaskDetail, TaskListItem } from "../types/api";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { signal });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(payload?.detail ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  listTasks: (signal?: AbortSignal) => getJson<TaskListItem[]>("/api/tasks", signal),
  getTask: (taskId: string, signal?: AbortSignal) =>
    getJson<TaskDetail>(`/api/tasks/${encodeURIComponent(taskId)}`, signal),
  getEvents: (taskId: string, signal?: AbortSignal) =>
    getJson<PlatformEvent[]>(`/api/tasks/${encodeURIComponent(taskId)}/events`, signal),
  getDiff: (taskId: string, signal?: AbortSignal) =>
    getJson<DiffResponse>(`/api/tasks/${encodeURIComponent(taskId)}/diff`, signal),
};
