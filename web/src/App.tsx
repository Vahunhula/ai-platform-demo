import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api/client";
import { usePresence } from "./api/usePresence";
import { type StreamState, useTaskStream } from "./api/useTaskStream";
import { ChatPanel } from "./components/ChatPanel";
import { ModelRoutingPanel } from "./components/ModelRoutingPanel";
import { TaskControls } from "./components/TaskControls";
import { TaskSidebar } from "./components/TaskSidebar";
import { NewTaskDialog } from "./components/NewTaskDialog";
import { ActivityTab, DiffTab, SummaryTab, TestsTab, WorkspaceTab } from "./components/tabs";
import { WorkflowProgress } from "./components/WorkflowProgress";
import { TaskTabs, type TaskTab } from "./components/TaskTabs";
import type {
  ConfigResponse,
  ConversationMessage,
  CurrentUser,
  PlatformEvent,
  TaskDetail,
  TaskListItem,
  CreateTaskResponse,
} from "./types/api";

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
const ACTIVITY_DOM_LIMIT = 200;
const DERIVED_EVENT_WINDOW = 500;

function message(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

function isAbort(reason: unknown): boolean {
  return reason instanceof DOMException && reason.name === "AbortError";
}

function mergeEvents(current: PlatformEvent[], incoming: PlatformEvent[]): PlatformEvent[] {
  const bySequence = new Map(current.map((event) => [event.sequence_id, event]));
  for (const event of incoming) bySequence.set(event.sequence_id, event);
  return [...bySequence.values()]
    .sort((left, right) => left.sequence_id - right.sequence_id)
    .slice(-DERIVED_EVENT_WINDOW);
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
  const [activityEvents, setActivityEvents] = useState<PlatformEvent[]>([]);
  const [activityBefore, setActivityBefore] = useState<number | null>(null);
  const [activityAtNewest, setActivityAtNewest] = useState(true);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [messages, setMessages] = useState<ConversationMessage[] | null>(null);
  // null = not loaded yet for the selected task; "" = loaded and empty.
  const [diff, setDiff] = useState<string | null>(null);
  // Sequence the live stream starts after; null until the task's history is loaded.
  const [streamAfter, setStreamAfter] = useState<number | null>(null);
  const [tab, setTab] = useState<TaskTab>("Chat");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const selectedRef = useRef<string | null>(null);
  selectedRef.current = selectedId;
  const activityAtNewestRef = useRef(activityAtNewest);
  activityAtNewestRef.current = activityAtNewest;
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
      api.getActivityPage(selectedId, undefined, controller.signal),
      api.getMessages(selectedId, controller.signal),
    ])
      .then(([nextDetail, activityPage, nextMessages]) => {
        if (selectedRef.current !== selectedId) return;
        setDetail(nextDetail);
        setEvents([...activityPage.items].sort((left, right) => left.sequence_id - right.sequence_id));
        setActivityEvents(activityPage.items);
        setActivityBefore(activityPage.next_before_sequence);
        setActivityAtNewest(true);
        setMessages(nextMessages);
        setDiff(null);
        setStreamAfter(activityPage.items[0]?.sequence_id ?? 0);
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
      if (activityAtNewestRef.current) {
        setActivityEvents((current) => {
          const next = [...new Map([event, ...current].map((item) => [item.sequence_id, item])).values()]
            .sort((left, right) => right.sequence_id - left.sequence_id)
            .slice(0, ACTIVITY_DOM_LIMIT);
          if (next.length === ACTIVITY_DOM_LIMIT) {
            setActivityBefore(next[next.length - 1].sequence_id);
          }
          return next;
        });
      }
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
        const [activityPage] = await Promise.all([
          api.getActivityPage(selectedId),
          refreshSelected(selectedId),
        ]);
        if (selectedRef.current === selectedId) {
          setEvents((current) => mergeEvents(current, activityPage.items));
          setActivityEvents(activityPage.items);
          setActivityBefore(activityPage.next_before_sequence);
          setActivityAtNewest(true);
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
    setActivityEvents([]);
    setActivityBefore(null);
    setActivityAtNewest(true);
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
    setActivityEvents([]);
    setActivityBefore(null);
    setActivityAtNewest(true);
    setMessages(null);
    setDiff(null);
    setStreamAfter(null);
    void loadTasks();
  };

  const loadOlderActivity = async () => {
    if (!selectedId || activityBefore === null || loadingOlder) return;
    setLoadingOlder(true);
    try {
      const page = await api.getActivityPage(selectedId, activityBefore);
      if (selectedRef.current !== selectedId) return;
      setActivityEvents((current) => {
        const combined = [...new Map([...current, ...page.items].map((item) => [item.sequence_id, item])).values()]
          .sort((left, right) => right.sequence_id - left.sequence_id);
        if (combined.length > ACTIVITY_DOM_LIMIT) {
          setActivityAtNewest(false);
          return combined.slice(-ACTIVITY_DOM_LIMIT);
        }
        return combined;
      });
      setActivityBefore(page.next_before_sequence);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setLoadingOlder(false);
    }
  };

  const returnToNewestActivity = async () => {
    if (!selectedId) return;
    try {
      const page = await api.getActivityPage(selectedId);
      if (selectedRef.current !== selectedId) return;
      setActivityEvents(page.items);
      setActivityBefore(page.next_before_sequence);
      setActivityAtNewest(true);
    } catch (reason) {
      setError(message(reason));
    }
  };

  return (
    <div className="app-shell">
      <div className="workspace">
        <TaskSidebar
          tasks={tasks}
          selectedId={selectedId}
          onSelect={selectTask}
          onNewTask={() => setCreating(true)}
          canCreate={user.can_modify_tasks}
          user={user}
          onSignOut={onSignOut}
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
                  <div className="task-header-meta">
                    <span className={`hero-status status-${detail.status.toLowerCase()}`}>{detail.status.replaceAll("_", " ")}</span>
                    <strong>{detail.workflow_phase.replaceAll("_", " ")}</strong>
                    <span>Repository <b>{detail.repository_id ?? "Not set"}</b></span>
                    <span>Base <b>{detail.base_branch ?? "Not set"}</b></span>
                    <span>Assignee <b>{detail.assignee_display_name ?? "Unassigned"}</b></span>
                    {detail.latest_readiness && <span>Readiness <b>{detail.latest_readiness.score.toFixed(0)}%</b></span>}
                  </div>
                </div>
                <div className="header-side">
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
                  {streamState !== "idle" && <span className={`stream-state stream-${streamState}`}>{STREAM_LABEL[streamState]}</span>}
                  <button className="icon-button" onClick={refresh} disabled={loading} aria-label="Refresh task">↻</button>
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
                </div>
                <TaskControls
                  detail={detail}
                  onChanged={() => scheduleRefresh(detail.id)}
                  onRemoved={taskRemoved}
                />
              </section>

              <WorkflowProgress detail={detail} events={events} />

              <section className="activity-panel">
                <TaskTabs active={tab} onChange={setTab} />
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
                  {tab === "Tests" && <TestsTab taskId={detail.id} events={events} />}
                  {tab === "Activity" && (
                    <ActivityTab
                      events={activityEvents}
                      hasOlder={activityBefore !== null}
                      loadingOlder={loadingOlder}
                      atNewest={activityAtNewest}
                      onLoadOlder={() => void loadOlderActivity()}
                      onReturnNewest={() => void returnToNewestActivity()}
                    />
                  )}
                  {tab === "Summary" && <SummaryTab detail={detail} messages={messages} diff={diff} />}
                  {tab === "Workspace" && <><WorkspaceTab detail={detail} /><ModelRoutingPanel taskId={detail.id} user={user} /></>}
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
