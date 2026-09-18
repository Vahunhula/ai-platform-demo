import { useCallback, useEffect, useState } from "react";

import { api } from "./api/client";
import { TaskOverview } from "./components/TaskOverview";
import { TaskSidebar } from "./components/TaskSidebar";
import { ChatTab, DiffTab, TestsTab, TraceTab } from "./components/tabs";
import type { PlatformEvent, TaskDetail, TaskListItem } from "./types/api";

type Tab = "Chat" | "Diff" | "Tests" | "Trace";

const tabs: Tab[] = ["Chat", "Diff", "Tests", "Trace"];

function message(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

function App() {
  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [events, setEvents] = useState<PlatformEvent[]>([]);
  // null = not loaded yet for the selected task; "" = loaded and empty.
  const [diff, setDiff] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("Chat");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadTasks = useCallback(async (signal?: AbortSignal) => {
    const nextTasks = await api.listTasks(signal);
    setTasks(nextTasks);
    setSelectedId((current) => current ?? nextTasks[0]?.id ?? null);
  }, []);

  const loadSelected = useCallback(async (taskId: string, signal?: AbortSignal) => {
    const [nextDetail, nextEvents] = await Promise.all([
      api.getTask(taskId, signal),
      api.getEvents(taskId, signal),
    ]);
    setDetail(nextDetail);
    setEvents(nextEvents);
    setDiff(null);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    loadTasks(controller.signal)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(message(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [loadTasks]);

  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    loadSelected(selectedId, controller.signal)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(message(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [loadSelected, selectedId]);

  useEffect(() => {
    if (tab !== "Diff" || !detail || diff !== null) return;
    const controller = new AbortController();
    api
      .getDiff(detail.id, controller.signal)
      .then((response) => setDiff(response.diff))
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(message(reason));
      });
    return () => controller.abort();
  }, [detail, diff, tab]);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      await loadTasks();
      if (selectedId) await loadSelected(selectedId);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setLoading(false);
    }
  };

  const selectTask = (taskId: string) => {
    if (taskId === selectedId) return;
    setDetail(null);
    setEvents([]);
    setDiff(null);
    setSelectedId(taskId);
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <span className="eyebrow">Read-only view · control via CLI</span>
          <h1>AI Platform</h1>
        </div>
        <button className="refresh" onClick={refresh} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </button>
      </header>

      <div className="workspace">
        <TaskSidebar tasks={tasks} selectedId={selectedId} onSelect={selectTask} />

        <main className="main-content">
          {error && <div className="error-banner">{error}</div>}
          {!detail && !error && (
            <div className="empty-state">{loading ? "Loading…" : "Select a task to inspect it."}</div>
          )}
          {detail && (
            <>
              <section className="task-header">
                <div>
                  <span className="task-kicker">{detail.id}</span>
                  <h2>{detail.title}</h2>
                </div>
                <span className={`hero-status status-${detail.status.toLowerCase()}`}>
                  {detail.status}
                </span>
              </section>

              <TaskOverview detail={detail} />

              <section className="activity-panel">
                <div className="tabs" role="tablist" aria-label="Task activity">
                  {tabs.map((item) => (
                    <button
                      key={item}
                      role="tab"
                      aria-selected={tab === item}
                      className={tab === item ? "active" : ""}
                      onClick={() => setTab(item)}
                    >
                      {item}
                    </button>
                  ))}
                </div>
                <div className="tab-body" role="tabpanel">
                  {tab === "Chat" && <ChatTab events={events} />}
                  {tab === "Diff" && (
                    <DiffTab diff={diff} workspaceExists={detail.workspace_id !== null} />
                  )}
                  {tab === "Tests" && <TestsTab events={events} />}
                  {tab === "Trace" && <TraceTab events={events} />}
                </div>
              </section>
            </>
          )}
        </main>
      </div>
    </div>
  );
}

export default App;
