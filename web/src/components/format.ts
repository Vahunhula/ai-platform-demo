import type { PlatformEvent } from "../types/api";

export function formatTime(timestamp: string): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(timestamp));
}

export function formatDateTime(timestamp: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(timestamp));
}

function text(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  return typeof value === "string" ? value : JSON.stringify(value);
}

function upper(value: unknown): string | null {
  const result = text(value);
  return result ? result.toUpperCase() : null;
}

function joined(...parts: (string | null)[]): string | null {
  const present = parts.filter((part): part is string => Boolean(part));
  return present.length ? present.join(" · ") : null;
}

/** Render a persisted verification command without the interpreter's absolute path. */
export function formatCommand(value: unknown): string | null {
  if (!Array.isArray(value)) return text(value);
  const [interpreter, ...args] = value.map(String);
  const executable = interpreter?.split("/").pop() ?? "";
  return [executable, ...args].join(" ");
}

/** One human-readable line describing a public platform event, from persisted metadata only. */
export function summarizeEvent(event: PlatformEvent): string | null {
  const m = event.metadata;
  switch (event.event_type) {
    case "TASK_CREATED":
      return text(m.title);
    case "MODEL_SELECTED":
      return joined(`${text(m.tier) ?? "?"} / ${text(m.model) ?? "?"}`, text(m.reason));
    case "MODEL_ESCALATED":
      return joined(
        `${text(m.previous_tier)} / ${text(m.previous_model)} → ${text(m.new_tier)} / ${text(m.new_model)}`,
        text(m.reason),
      );
    case "STATUS_CHANGED":
    case "HUMAN_PAUSED":
      return m.from || m.to ? `${upper(m.from) ?? "?"} → ${upper(m.to) ?? "?"}` : null;
    case "AGENT_STARTED":
      return joined(
        m.tier || m.model ? `${text(m.tier) ?? "?"} / ${text(m.model) ?? "?"}` : null,
        m.attempt !== undefined ? `attempt ${text(m.attempt)}` : null,
        m.continuation ? "continuation" : null,
      );
    case "AGENT_COMPLETED":
      return joined(text(m.summary), m.turns !== undefined ? `${text(m.turns)} turns` : null);
    case "AGENT_FAILED":
    case "TASK_FAILED":
      return text(m.error)?.split("\n")[0] ?? text(m.summary);
    case "AGENT_TOOL_ACTIVITY":
      return joined(text(m.tool), text(m.summary));
    case "AGENT_MESSAGE":
    case "HUMAN_MESSAGE":
      return text(m.message);
    case "FILE_CHANGED":
      return joined(text(m.change_type), text(m.path));
    case "HUMAN_WORKSPACE_CHANGED":
      return Array.isArray(m.files)
        ? m.files
            .map((file: { change_type?: string; path?: string }) => `${file.change_type} ${file.path}`)
            .join(", ")
        : null;
    case "TEST_STARTED":
      return formatCommand(m.command);
    case "TEST_PASSED":
    case "TEST_FAILED":
      return joined(
        m.exit_code !== undefined ? `exit ${text(m.exit_code)}` : null,
        typeof m.duration_seconds === "number" ? `${m.duration_seconds.toFixed(2)}s` : null,
        m.timed_out ? "timed out" : null,
      );
    case "HUMAN_SHELL_CLOSED":
      return m.exit_code !== undefined ? `exit ${text(m.exit_code)}` : null;
    case "TASK_COMPLETED":
      return m.workspace_retained ? "workspace retained" : null;
    case "STALE_LOCK_RECOVERED":
      return text(m.reason);
    default:
      return null;
  }
}
