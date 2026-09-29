import { useEffect, useState, type ReactNode } from "react";

import { api } from "../api/client";
import type { ChecklistEvaluation, ConversationMessage, PlatformEvent, TaskDetail, TaskTests } from "../types/api";
import { formatCommand, formatDateTime, formatTime, summarizeEvent } from "./format";

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

export function TestsTab({ taskId, events }: { taskId: string; events: PlatformEvent[] }) {
  const [owned, setOwned] = useState<TaskTests | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setOwned(null);
    api.getTaskTests(taskId, controller.signal).then(setOwned).catch(() => setOwned(null));
    return () => controller.abort();
  }, [taskId, events]);
  const tests = events.filter((event) => TEST_EVENTS.has(event.event_type));
  return (
    <div className="event-list tests-view">
      <TestSection title="Human requirements">
        {owned?.human_requirements ? (
          <pre className="test-evidence">{owned.human_requirements}</pre>
        ) : <p className="muted">Not provided.</p>}
        {owned && owned.generation_status !== "NOT_REQUESTED" && (
          <p className="muted">Generation: {owned.generation_status}{owned.generation_message ? ` — ${owned.generation_message}` : ""}</p>
        )}
      </TestSection>
      <TestFileSection title="Generated acceptance tests" files={owned?.generated_tests ?? []} />
      <TestFileSection title="Uploaded test files" files={owned?.uploaded_tests ?? []} />
      <TestSection title="Repository tests">
        {owned?.repository_tests.length ? <ul>{owned.repository_tests.map((path) => <li key={path}><code>{path}</code></li>)}</ul> : <p className="muted">None found.</p>}
      </TestSection>
      <h3>Verification</h3>
      {!tests.length && <p className="muted">No persisted verification events yet.</p>}
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
                {m.verification_mode === "BASELINE_AWARE" && (
                  <>
                    <dt>Task tests</dt>
                    <dd>
                      {String(m.task_acceptance_status ?? "NOT_PROVIDED")}
                      {typeof m.task_specific_passed_tests === "number" &&
                        ` · ${m.task_specific_passed_tests} passed`}
                    </dd>
                    <dt>Broad regression</dt>
                    <dd>{m.broad_regression_passed === true ? "PASS" : "FAIL"}</dd>
                    <dt>New regressions</dt>
                    <dd>{String(m.new_regression_count ?? 0)}</dd>
                    <dt>Baseline warnings</dt>
                    <dd>{String(m.baseline_warning_count ?? 0)}</dd>
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

function TestSection({ title, children }: { title: string; children: ReactNode }) {
  return <section><h3>{title}</h3>{children}</section>;
}

function TestFileSection({ title, files }: { title: string; files: TaskTests["generated_tests"] }) {
  return (
    <TestSection title={title}>
      {files.length === 0 ? <p className="muted">None.</p> : files.map((file) => (
        <details key={file.relative_path}>
          <summary><code>{file.relative_path}</code> · {file.source.toLowerCase()}</summary>
          <pre className="test-evidence">{file.content}</pre>
        </details>
      ))}
    </TestSection>
  );
}

export function ActivityTab({
  events,
  hasOlder = false,
  loadingOlder = false,
  atNewest = true,
  onLoadOlder,
  onReturnNewest,
}: {
  events: PlatformEvent[];
  hasOlder?: boolean;
  loadingOlder?: boolean;
  atNewest?: boolean;
  onLoadOlder?: () => void;
  onReturnNewest?: () => void;
}) {
  if (!events.length) return <Empty text="No events recorded yet." />;
  const ordered = [...events].sort((left, right) => right.sequence_id - left.sequence_id);
  return (
    <div className="event-list">
      <div className="tab-note activity-controls">
        <span>
          {events.length} loaded public platform events, newest first. Model reasoning is never recorded here.
        </span>
        {!atNewest && onReturnNewest && (
          <button type="button" className="link-button" onClick={onReturnNewest}>
            Return to newest
          </button>
        )}
      </div>
      {ordered.map((event) => (
        <ActivityEvent event={event} key={event.sequence_id} />
      ))}
      {hasOlder && onLoadOlder && (
        <button
          type="button"
          className="link-button load-more"
          onClick={onLoadOlder}
          disabled={loadingOlder}
        >
          {loadingOlder ? "Loading…" : "Load older events"}
        </button>
      )}
    </div>
  );
}

function ActivityEvent({ event }: { event: PlatformEvent }) {
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
        <span className="actor" title={event.actor_id}>
          {event.actor_display_name} <span className="muted">({event.actor_type})</span>
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

const STATUS_LABEL: Record<string, string> = { PASS: "Pass", FAIL: "Fail", NEEDS_HUMAN: "Needs human" };

function ReadinessChecklist({ evaluation }: { evaluation: ChecklistEvaluation | null }) {
  if (!evaluation) return <p className="muted">No readiness gate has run for this task yet.</p>;
  return (
    <div className="readiness-checklist">
      <div className="readiness-score">
        <strong>{evaluation.readiness.score.toFixed(0)}%</strong>
        <span className="muted">
          {evaluation.readiness.eligible_for_auto_progression
            ? "eligible to auto-progress (≥ 98%, no blockers)"
            : "blocked from auto-progressing"}
        </span>
      </div>
      <ul>
        {evaluation.items.map((item) => (
          <li key={item.key}>
            <span className={`status-pill status-${item.status.toLowerCase()}`}>
              {STATUS_LABEL[item.status] ?? item.status}
            </span>
            <span>{item.label}</span>
            {item.blocking && <span className="muted blocking-flag">blocking</span>}
            <span className="muted weight">weight {item.weight}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Latest phase_result Chat item per artifact kind, i.e. the current artifacts. */
function latestArtifacts(messages: ConversationMessage[] | null): ConversationMessage[] {
  if (!messages) return [];
  const byKind = new Map<string, ConversationMessage>();
  for (const message of messages) {
    if (message.type !== "phase_result" || !message.artifact_kind) continue;
    const existing = byKind.get(message.artifact_kind);
    if (!existing || message.sequence_id > existing.sequence_id) byKind.set(message.artifact_kind, message);
  }
  return [...byKind.values()].sort((a, b) => a.sequence_id - b.sequence_id);
}

export function SummaryTab({
  detail,
  messages,
  diff,
}: {
  detail: TaskDetail;
  messages: ConversationMessage[] | null;
  diff: string | null;
}) {
  const [evaluation, setEvaluation] = useState<ChecklistEvaluation | null | undefined>(undefined);

  useEffect(() => {
    setEvaluation(undefined);
    const controller = new AbortController();
    api
      .getChecklists(detail.id, controller.signal)
      .then((evaluations) => {
        const forPhase = [...evaluations].reverse().find((item) => item.phase === detail.workflow_phase);
        setEvaluation(forPhase ?? evaluations.at(-1) ?? null);
      })
      .catch(() => setEvaluation(null));
    return () => controller.abort();
  }, [detail.id, detail.workflow_phase]);

  const pendingQuestion = [...(messages ?? [])].reverse().find((m) => m.type === "human_input_required");
  const changedFileCount = diff ? new Set(diff.match(/^\+\+\+ .+$/gm) ?? []).size : null;

  return (
    <div className="summary-tab">
      <div className="facts">
        <SummaryFact label="Task" value={`${detail.id} · ${detail.title}`} />
        <SummaryFact label="Repository" value={detail.repository_id ?? "Not set"} />
        <SummaryFact label="Base branch" value={detail.base_branch ?? "Not set"} />
        <SummaryFact label="Lifecycle" value={detail.status} />
        <SummaryFact label="Workflow phase" value={detail.workflow_phase.replaceAll("_", " ")} />
        <SummaryFact label="Disposition" value={detail.disposition ?? "None"} />
        <SummaryFact
          label="Model"
          value={[detail.model_tier, detail.model_name].filter(Boolean).join(" / ") || "Not selected"}
        />
        <SummaryFact label="Verification" value={detail.verification_status} />
        <SummaryFact
          label="Changed files"
          value={changedFileCount === null ? "Unknown" : String(changedFileCount)}
        />
        <SummaryFact label="Pending instructions" value={String(detail.queued_messages)} />
      </div>

      <section>
        <h3>Readiness</h3>
        {evaluation === undefined ? <p className="muted">Loading…</p> : <ReadinessChecklist evaluation={evaluation} />}
      </section>

      {detail.status === "WAITING_FOR_HUMAN" && (
        <section>
          <h3>Pending question</h3>
          {pendingQuestion ? (
            <p className="phase-result-body">{pendingQuestion.content}</p>
          ) : (
            <p className="muted">This task is waiting for human input.</p>
          )}
        </section>
      )}

      <section>
        <h3>Latest artifacts</h3>
        {latestArtifacts(messages).length === 0 ? (
          <p className="muted">No phase output yet.</p>
        ) : (
          <ul className="artifact-list">
            {latestArtifacts(messages).map((artifact) => (
              <li key={artifact.id}>
                <strong>{artifact.title}</strong>{" "}
                <span className="muted">v{artifact.artifact_version}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function SummaryFact({ label, value }: { label: string; value: string }) {
  return (
    <div className="fact">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function WorkspaceTab({ detail }: { detail: TaskDetail }) {
  return (
    <div className="summary-tab">
      <div className="facts">
        <SummaryFact label="Repository" value={detail.repository_id ?? "Not set"} />
        <SummaryFact label="Base branch" value={detail.base_branch ?? "Not set"} />
        <SummaryFact label="Workspace" value={detail.workspace_id ?? "Not created"} />
        <SummaryFact label="Executor state" value={detail.agent_working ? "Running" : "Idle"} />
        <SummaryFact label="Active writer" value={detail.writer ?? "None"} />
        <SummaryFact label="Attempt" value={String(detail.current_attempt)} />
        <SummaryFact label="Verification" value={detail.verification_status} />
      </div>
      <p className="muted timestamps">
        Created {formatDateTime(detail.created_at)}
        <br />
        Updated {formatDateTime(detail.updated_at)}
      </p>
      {!detail.workspace_id && <Empty text="No workspace has been created for this task yet." />}
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="empty-state">{text}</div>;
}
