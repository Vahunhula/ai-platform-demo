import { useState } from "react";

import type { PlatformEvent } from "../types/api";
import { formatCommand, formatTime, summarizeEvent } from "./format";

export function DiffTab({
  diff,
  workspaceExists,
}: {
  diff: string | null;
  workspaceExists: boolean;
}) {
  if (diff === null) return <Empty text="Loading diff…" />;
  if (!workspaceExists) return <Empty text="No workspace exists for this task yet." />;
  if (!diff.trim()) return <Empty text="Workspace is clean." />;
  return (
    <pre className="code-block diff">
      {diff.split("\n").map((line, index) => (
        <span key={index} className={diffLineClass(line)}>
          {line + "\n"}
        </span>
      ))}
    </pre>
  );
}

function diffLineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ")) return "diff-file";
  if (line.startsWith("@@")) return "diff-hunk";
  if (line.startsWith("+")) return "diff-add";
  if (line.startsWith("-")) return "diff-del";
  return "";
}

const TEST_EVENTS = new Set(["TEST_STARTED", "TEST_PASSED", "TEST_FAILED"]);

export function TestsTab({ events }: { events: PlatformEvent[] }) {
  const tests = events.filter((event) => TEST_EVENTS.has(event.event_type));
  if (!tests.length) return <Empty text="No persisted verification events yet." />;
  return (
    <div className="event-list">
      {tests.map((event) => {
        const m = event.metadata;
        const output = [m.stdout, m.stderr, m.error].filter(Boolean).map(String).join("\n");
        return (
          <article className={`event test-${event.event_type.toLowerCase()}`} key={event.sequence_id}>
            <span className="sequence">#{event.sequence_id}</span>
            <div>
              <div className="event-heading">
                <strong>{event.event_type}</strong>
                <time>{formatTime(event.timestamp)}</time>
              </div>
              <span className="actor">
                {event.actor_id}
                {m.attempt !== undefined && ` · attempt ${String(m.attempt)}`}
              </span>
              <dl className="test-facts">
                {m.command !== undefined && (
                  <>
                    <dt>Command</dt>
                    <dd>
                      <code>{formatCommand(m.command)}</code>
                    </dd>
                  </>
                )}
                {m.exit_code !== undefined && (
                  <>
                    <dt>Exit code</dt>
                    <dd>{String(m.exit_code)}</dd>
                  </>
                )}
                {typeof m.duration_seconds === "number" && (
                  <>
                    <dt>Duration</dt>
                    <dd>{m.duration_seconds.toFixed(2)}s</dd>
                  </>
                )}
                {m.timed_out === true && (
                  <>
                    <dt>Timed out</dt>
                    <dd>yes</dd>
                  </>
                )}
              </dl>
              {output && (
                <details>
                  <summary>Persisted output</summary>
                  <pre className="test-evidence">{output}</pre>
                </details>
              )}
            </div>
          </article>
        );
      })}
    </div>
  );
}

export function TraceTab({ events }: { events: PlatformEvent[] }) {
  if (!events.length) return <Empty text="No events recorded yet." />;
  return (
    <div className="event-list">
      <p className="tab-note">
        {events.length} public platform events in sequence order. Model reasoning is never
        recorded here.
      </p>
      {events.map((event) => (
        <TraceEvent event={event} key={event.sequence_id} />
      ))}
    </div>
  );
}

function TraceEvent({ event }: { event: PlatformEvent }) {
  const [open, setOpen] = useState(false);
  const summary = summarizeEvent(event);
  const hasMetadata = Object.keys(event.metadata).length > 0;
  return (
    <article className="event">
      <span className="sequence">#{event.sequence_id}</span>
      <div>
        <div className="event-heading">
          <strong>{event.event_type}</strong>
          <time>{formatTime(event.timestamp)}</time>
        </div>
        <span className="actor">
          {event.actor_id} <span className="muted">({event.actor_type})</span>
        </span>
        {summary && <p className="event-summary">{summary}</p>}
        {hasMetadata && (
          <button className="link-button" onClick={() => setOpen((value) => !value)}>
            {open ? "Hide details" : "Details"}
          </button>
        )}
        {open && <pre>{JSON.stringify(event.metadata, null, 2)}</pre>}
      </div>
    </article>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="empty-state">{text}</div>;
}
