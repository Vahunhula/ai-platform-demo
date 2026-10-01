import { useMemo, useState } from "react";
import type { CurrentUser, TaskListItem } from "../types/api";
import { formatRelative } from "./format";

interface Props {
  tasks: TaskListItem[];
  selectedId: string | null;
  onSelect: (taskId: string) => void;
  onNewTask: () => void;
  canCreate: boolean;
  user: CurrentUser;
  onSignOut: () => void;
}

const PHASE: Record<string, string> = {
  BRAINSTORM: "Brainstorm", PLAN: "Plan", IMPLEMENTATION: "Implementation",
  REVIEW: "Review", HUMAN_REVIEW: "Human Review",
};

function stateLabel(task: TaskListItem): string {
  if (task.status === "WAITING_FOR_HUMAN") return "Waiting for human";
  if (task.status === "COMPLETED") return task.disposition === "DEFERRED" ? "Deferred" : "Completed";
  if (["ANALYZING", "IMPLEMENTING", "VERIFYING"].includes(task.status)) return "Running";
  return task.status.replaceAll("_", " ").toLowerCase().replace(/^./, (letter) => letter.toUpperCase());
}

export function TaskSidebar({ tasks, selectedId, onSelect, onNewTask, canCreate, user, onSignOut }: Props) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => {
    const value = query.trim().toLowerCase();
    return value ? tasks.filter((task) => `${task.id} ${task.title} ${task.status} ${task.workflow_phase}`.toLowerCase().includes(value)) : tasks;
  }, [query, tasks]);
  return (
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark" aria-hidden="true">AI</span><strong>AI Platform</strong></div>
      <button className="new-task-button" onClick={onNewTask} disabled={!canCreate}>+ New Task</button>
      <div className="sidebar-heading">
        <h2>Tasks</h2>
        <span>{tasks.length}</span>
      </div>
      {tasks.length > 5 && <input className="task-search" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter tasks…" aria-label="Filter tasks" />}
      <nav aria-label="Tasks">
        {filtered.map((task) => (
          <button
            key={task.id}
            className={`task-card ${selectedId === task.id ? "selected" : ""}`}
            aria-current={selectedId === task.id ? "true" : undefined}
            onClick={() => onSelect(task.id)}
          >
            <span className="task-row">
              <strong>{task.id}</strong>
              <span className={`status-dot status-${task.status.toLowerCase()}`} aria-label={stateLabel(task)} />
            </span>
            <span className="task-title">{task.title}</span>
            <span className="task-meta"><span>{PHASE[task.workflow_phase]}</span><span>{stateLabel(task)}</span></span>
            <span className="task-activity">Updated {formatRelative(task.updated_at)}</span>
          </button>
        ))}
        {filtered.length === 0 && <p className="sidebar-empty">No matching tasks</p>}
      </nav>
      <div className="sidebar-account">
        <span className="avatar">{user.display_name.slice(0, 2).toUpperCase()}</span>
        <span><strong>{user.display_name}</strong><small>{user.role}</small></span>
        <button onClick={onSignOut}>Log out</button>
      </div>
    </aside>
  );
}
