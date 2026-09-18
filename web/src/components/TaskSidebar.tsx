import type { TaskListItem } from "../types/api";

interface Props {
  tasks: TaskListItem[];
  selectedId: string | null;
  onSelect: (taskId: string) => void;
}

export function TaskSidebar({ tasks, selectedId, onSelect }: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar-heading">
        <h2>Tasks</h2>
        <span>{tasks.length}</span>
      </div>
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
          </button>
        ))}
      </nav>
    </aside>
  );
}
