import type { TaskListItem } from "../types/api";
import { formatRelative } from "./format";

interface Props {
  tasks: TaskListItem[];
  selectedId: string | null;
  onSelect: (taskId: string) => void;
  onNewTask: () => void;
  canCreate: boolean;
}

export function TaskSidebar({ tasks, selectedId, onSelect, onNewTask, canCreate }: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar-heading">
        <h2>Tasks</h2>
        <span>{tasks.length}</span>
      </div>
      <button className="new-task-button" onClick={onNewTask} disabled={!canCreate}>
        + New Task
      </button>
      <nav aria-label="Tasks">
        {tasks.map((task) => (
          <button
            key={task.id}
            className={`task-card ${selectedId === task.id ? "selected" : ""}`}
            aria-current={selectedId === task.id ? "true" : undefined}
            onClick={() => onSelect(task.id)}
          >
            <span className="task-row">
              <strong>{task.id}</strong>
              <span className={`status status-${task.status.toLowerCase()}`}>{task.status}</span>
            </span>
            <span className="task-title">{task.title}</span>
            <span className="difficulty">
              {task.difficulty} difficulty
              {task.model_tier && ` · ${task.model_tier}`}
              {task.writer && ` · writer: ${task.writer}`}
            </span>
            <span className="task-activity">updated {formatRelative(task.updated_at)}</span>
          </button>
        ))}
      </nav>
    </aside>
  );
}
