import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api/client";
import { usePresence } from "./api/usePresence";
import { type StreamState, useTaskStream } from "./api/useTaskStream";
import { ChatPanel } from "./components/ChatPanel";
import { TaskOverview } from "./components/TaskOverview";
import { ModelRoutingPanel } from "./components/ModelRoutingPanel";
import { TaskControls } from "./components/TaskControls";
import { TaskSidebar } from "./components/TaskSidebar";
import { NewTaskDialog } from "./components/NewTaskDialog";
import { DiffTab, SummaryTab, TestsTab, TraceTab, WorkspaceTab } from "./components/tabs";
import { WorkflowProgress } from "./components/WorkflowProgress";
import type {
  ConfigResponse,
  ConversationMessage,
  CurrentUser,
  PlatformEvent,
  TaskDetail,
  TaskListItem,
  CreateTaskResponse,
} from "./types/api";

type Tab = "Chat" | "Changes" | "Tests" | "Activity" | "Summary" | "Workspace";

const tabs: Tab[] = ["Chat", "Changes", "Tests", "Activity", "Summary", "Workspace"];
// Events after which the workspace diff may have changed.
const DIFF_EVENTS = new Set([
  "FILE_CHANGED",
  "HUMAN_WORKSPACE_CHANGED",
  "AGENT_COMPLETED",
  "AGENT_FAILED",
  "WORKSPACE_CREATED",
  "WORKSPACE_RESET",
]);
const REFRESH_DEBOUNCE_MS = 300;
const TASK_LIST_POLL_MS = 30_000;

function message(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

function isAbort(reason: unknown): boolean {
  return reason instanceof DOMException && reason.name === "AbortError";
}

function mergeEvents(current: PlatformEvent[], incoming: PlatformEvent[]): PlatformEvent[] {
  const bySequence = new Map(current.map((event) => [event.sequence_id, event]));
  for (const event of incoming) bySequence.set(event.sequence_id, event);
  return [...bySequence.values()].sort((left, right) => left.sequence_id - right.sequence_id);
}

const STREAM_LABEL: Record<StreamState, string> = {
  idle: "",
  connecting: "connecting…",
  live: "live",
  reconnecting: "reconnecting…",
};

function initials(name: string): string {
  const parts = name.trim().split(/\s+/);
  return (parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : name.slice(0, 2)).toUpperCase();
}

const ROLE_LABEL: Record<CurrentUser["role"], string> = {
  viewer: "Viewer",
  developer: "Developer",
  admin: "Admin",
};

interface AppProps {
  user: CurrentUser;
  onSignOut: () => void;
}

function App({ user, onSignOut }: AppProps) {
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [events, setEvents] = useState<PlatformEvent[]>([]);
  const [messages, setMessages] = useState<ConversationMessage[] | null>(null);
  // null = not loaded yet for the selected task; "" = loaded and empty.
  const [diff, setDiff] = useState<string | null>(null);
  // Sequence the live stream starts after; null until the task's history is loaded.
  const [streamAfter, setStreamAfter] = useState<number | null>(null);
  const [tab, setTab] = useState<Tab>("Chat");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const selectedRef = useRef<string | null>(null);
  selectedRef.current = selectedId;
  const refreshTimer = useRef<number | undefined>(undefined);

  const loadTasks = useCallback(async (signal?: AbortSignal) => {
    const nextTasks = await api.listTasks(signal);
    setTasks(nextTasks);
    setSelectedId((current) => current ?? nextTasks[0]?.id ?? null);
  }, []);

  /** Refetch the selected task's derived state; ignore answers for a task no longer shown. */
  const refreshSelected = useCallback(async (taskId: string) => {
    const [nextDetail, nextMessages] = await Promise.all([
      api.getTask(taskId),
      api.getMessages(taskId),
    ]);
    if (selectedRef.current !== taskId) return;
    setDetail(nextDetail);
    setMessages(nextMessages);
  }, []);

  const scheduleRefresh = useCallback(
    (taskId: string) => {
      window.clearTimeout(refreshTimer.current);
      refreshTimer.current = window.setTimeout(() => {
        refreshSelected(taskId).catch((reason: unknown) => setError(message(reason)));
        loadTasks().catch(() => undefined);
      }, REFRESH_DEBOUNCE_MS);
    },
    [loadTasks, refreshSelected],
  );

  useEffect(() => {
    const controller = new AbortController();
    api
      .getConfig(controller.signal)
      .then(setConfig)
      .catch((reason: unknown) => {
        if (!isAbort(reason)) setError(message(reason));
      });
    loadTasks(controller.signal)
      .catch((reason: unknown) => {
        if (!isAbort(reason)) setError(message(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    const poll = window.setInterval(() => loadTasks().catch(() => undefined), TASK_LIST_POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(poll);
    };
  }, [loadTasks]);

  // Load the selected task's full history, then start the live stream after it.
  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([
      api.getTask(selectedId, controller.signal),
      api.getEvents(selectedId, controller.signal),
      api.getMessages(selectedId, controller.signal),
    ])
      .then(([nextDetail, nextEvents, nextMessages]) => {
        if (selectedRef.current !== selectedId) return;
        setDetail(nextDetail);
        setEvents(nextEvents);
        setMessages(nextMessages);
        setDiff(null);
        setStreamAfter(nextEvents.at(-1)?.sequence_id ?? 0);
      })
      .catch((reason: unknown) => {
        if (!isAbort(reason)) setError(message(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [selectedId]);

  const viewers = usePresence(selectedId, config?.presence_heartbeat_seconds ?? 15);

  const streamState = useTaskStream(selectedId, streamAfter, {
    onEvent: (event) => {
      const taskId = selectedRef.current;
      if (!taskId) return;
      setEvents((current) => mergeEvents(current, [event]));
      if (DIFF_EVENTS.has(event.event_type)) setDiff(null);
      scheduleRefresh(taskId);
    },
    onConversation: () => {
      if (selectedRef.current) scheduleRefresh(selectedRef.current);
    },
  });

  useEffect(() => {
    if ((tab !== "Changes" && tab !== "Summary") || !detail || diff !== null) return;
    const controller = new AbortController();
    api
      .getDiff(detail.id, controller.signal)
      .then((response) => {
        if (selectedRef.current === response.task_id) setDiff(response.diff);
      })
      .catch((reason: unknown) => {
        if (!isAbort(reason)) setError(message(reason));
      });
    return () => controller.abort();
  }, [detail, diff, tab]);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      await loadTasks();
      if (selectedId) {
        const [nextEvents] = await Promise.all([
          api.getEvents(selectedId),
          refreshSelected(selectedId),
        ]);
        if (selectedRef.current === selectedId) {
          setEvents((current) => mergeEvents(current, nextEvents));
          setDiff(null);
        }
      }
    } catch (reason) {
      setError(message(reason));
    } finally {
      setLoading(false);
    }
  };

  const selectTask = (taskId: string) => {
    if (taskId === selectedId) return;
    window.clearTimeout(refreshTimer.current);
    setDetail(null);
    setEvents([]);
    setMessages(null);
    setDiff(null);
    setStreamAfter(null);
    setSelectedId(taskId);
  };

  const taskCreated = (task: CreateTaskResponse) => {
    setCreating(false);
    void loadTasks();
    selectTask(task.id);
  };

  const taskRemoved = () => {
    window.clearTimeout(refreshTimer.current);
    setSelectedId(null);
    setDetail(null);
    setEvents([]);
    setMessages(null);
    setDiff(null);
    setStreamAfter(null);
    void loadTasks();
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <span className="eyebrow">Shared TaskSessions</span>
          <h1>AI Platform</h1>
        </div>
        <div className="topbar-actions">
          <div className="current-user">
            <span className="avatar" aria-hidden="true">
              {initials(user.display_name)}
            </span>
            <span>
              <strong>{user.display_name}</strong>
              <small>
                {ROLE_LABEL[user.role]}
                {!user.can_modify_tasks && " · read-only access"}
              </small>
            </span>
            <button className="refresh" onClick={onSignOut}>
              Log out
            </button>
          </div>
          {streamState !== "idle" && (
            <span className={`stream-state stream-${streamState}`}>{STREAM_LABEL[streamState]}</span>
          )}
          <button className="refresh" onClick={refresh} disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </button>
        </div>
      </header>

      <div className="workspace">
        <TaskSidebar
          tasks={tasks}
          selectedId={selectedId}
          onSelect={selectTask}
          onNewTask={() => setCreating(true)}
          canCreate={user.can_modify_tasks}
        />

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
                <div className="header-side">
                  <span className={`hero-status status-${detail.status.toLowerCase()}`}>
                    {detail.status}
                  </span>
                  {viewers.length > 0 && (
                    <div
                      className="presence"
                      title={`${viewers.map((viewer) => viewer.display_name).join(", ")} viewing`}
                    >
                      <span className="muted">Viewing</span>
                      {viewers.map((viewer) => (
                        <span
                          key={viewer.user_id}
                          className={`avatar ${viewer.user_id === user.id ? "self" : ""}`}
                          aria-label={viewer.display_name}
                        >
                          {initials(viewer.display_name)}
                        </span>
                      ))}
                      <span className="presence-names">
                        {viewers.map((viewer) => viewer.display_name).join(" · ")}
                      </span>
                    </div>
                  )}
                </div>
              </section>

              <section className="task-state">
                <div className="state-chips">
                  {detail.agent_working && (
                    <span className="chip chip-working">
                      <span className="spinner" aria-hidden="true" /> Agent working
                    </span>
                  )}
                  {detail.pause_requested && <span className="chip chip-warn">Pause requested</span>}
                  {detail.queued_messages > 0 && (
                    <span className="chip chip-warn">{detail.queued_messages} queued message(s)</span>
                  )}
                  {detail.disposition && (
                    <span className="chip">Disposition: {detail.disposition}</span>
                  )}
                  <span className="chip">Writer: {detail.writer ?? "none"}</span>
                  <span className="chip">Verification: {detail.verification_status}</span>
                  <span className="chip">
                    Model: {[detail.model_tier, detail.model_name].filter(Boolean).join(" / ") || "not selected"}
                  </span>
                </div>
                <TaskControls
                  detail={detail}
                  onChanged={() => scheduleRefresh(detail.id)}
                  onRemoved={taskRemoved}
                />
              </section>

              <WorkflowProgress detail={detail} events={events} />
              <TaskOverview detail={detail} />
              <ModelRoutingPanel taskId={detail.id} user={user} />

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
                <div className={`tab-body ${tab === "Chat" ? "tab-chat" : ""}`} role="tabpanel">
                  {tab === "Chat" && (
                    <ChatPanel
                      detail={detail}
                      config={config}
                      user={user}
                      messages={messages}
                      events={events}
                      onSubmitted={() => scheduleRefresh(detail.id)}
                    />
                  )}
                  {tab === "Changes" && (
                    <DiffTab diff={diff} workspaceExists={detail.workspace_id !== null} />
                  )}
                  {tab === "Tests" && <TestsTab events={events} />}
                  {tab === "Activity" && <TraceTab events={events} />}
                  {tab === "Summary" && <SummaryTab detail={detail} messages={messages} diff={diff} />}
                  {tab === "Workspace" && <WorkspaceTab detail={detail} />}
                </div>
              </section>
            </>
          )}
        </main>
      </div>
      {creating && <NewTaskDialog onClose={() => setCreating(false)} onCreated={taskCreated} />}
    </div>
  );
}

export default App;
