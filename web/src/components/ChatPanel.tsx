import { type KeyboardEvent, useEffect, useLayoutEffect, useRef, useState } from "react";

import { ApiError, api } from "../api/client";
import { newClientId } from "../api/ids";
import type {
  CommandMetadata,
  ClaudeCommandMetadata,
  CommandResult,
  ConfigResponse,
  ConversationMessage,
  CurrentUser,
  PlatformEvent,
  TaskDetail,
} from "../types/api";
import { formatTime } from "./format";

/** A message this browser tab sent that the server's conversation does not show yet. */
interface PendingMessage {
  clientMessageId: string;
  text: string;
  state: "sending" | "accepted" | "failed";
  error?: string;
}

const STATUS_LABEL: Record<string, string> = {
  QUEUED: "Queued — runs after the current agent turn",
  RUNNING: "Agent turn running",
  FAILED: "Failed",
};

interface Props {
  detail: TaskDetail;
  config: ConfigResponse | null;
  user: CurrentUser;
  messages: ConversationMessage[] | null;
  events: PlatformEvent[];
  onSubmitted: () => void;
}

const STATUS_PILL_LABEL: Record<string, string> = {
  PASS: "Pass",
  FAIL: "Fail",
  NEEDS_HUMAN: "Needs human",
};

/** One Chat-timeline entry, shaped by the backend-owned ``type`` discriminator. */
function ChatEntry({
  message,
  own,
  answered,
}: {
  message: ConversationMessage;
  own: boolean;
  answered?: boolean;
}) {
  const heading = (
    <div className="event-heading">
      <strong title={message.actor_id}>
        {message.actor_display_name}
        {message.role === "agent" && message.type !== "phase_result" && (
          <span className="muted"> (agent)</span>
        )}
      </strong>
      <time>
        #{message.sequence_id} · {formatTime(message.timestamp)}
      </time>
    </div>
  );

  if (message.type === "platform_activity") {
    return (
      <div className="chat-activity" key={message.id}>
        <span>{message.content}</span>
        <time>{formatTime(message.timestamp)}</time>
      </div>
    );
  }

  if (message.type === "phase_result") {
    return (
      <article className="message phase-result">
        <div className="event-heading">
          <span><small className="artifact-label">{message.workflow_phase?.replaceAll("_", " ") ?? "Workflow artifact"}</small><strong>{message.title}</strong></span>
          <time>
            #{message.sequence_id} · {formatTime(message.timestamp)}
          </time>
        </div>
        <p className="phase-result-body">{message.content}</p>
        {message.readiness_score !== null && <div className="artifact-readiness">Readiness <strong>{message.readiness_score.toFixed(0)}%</strong></div>}
        {(message.artifact_version || message.concrete_model) && (
          <div className="phase-result-meta">
            {message.artifact_version && <span>v{message.artifact_version}</span>}
            {message.concrete_model && <span>{message.logical_model ?? message.concrete_model}</span>}
          </div>
        )}
      </article>
    );
  }

  if (message.type === "human_input_required") {
    return (
      <article className={`message needs-input ${answered ? "answered" : ""}`}>
        <div className="event-heading">
          <strong>{message.title}</strong>
          <time>
            #{message.sequence_id} · {formatTime(message.timestamp)}
          </time>
        </div>
        <p className="phase-result-body">{message.content}</p>
        {message.blocking_checks && message.blocking_checks.length > 0 && (
          <ul className="blocking-checks">
            {message.blocking_checks.map((check) => (
              <li key={check.key}>
                <span className={`status-pill status-${check.status.toLowerCase()}`}>
                  {STATUS_PILL_LABEL[check.status] ?? check.status}
                </span>
                {check.label}
              </li>
            ))}
          </ul>
        )}
        <span className={`chip ${answered ? "chip-ok" : "chip-warn"}`}>
          {answered ? "Answered" : "Needs your input"}
        </span>
      </article>
    );
  }

  // human_message, agent_message, command_result
  return (
    <article className={`message ${message.role} ${own ? "own" : ""}`}>
      {heading}
      <p>{message.content}</p>
      {message.status && message.status !== "COMPLETED" && (
        <span className={`delivery delivery-${message.status.toLowerCase()}`}>
          {STATUS_LABEL[message.status]}
          {message.error && `: ${message.error}`}
        </span>
      )}
    </article>
  );
}

export function ChatPanel({ detail, config, user, messages, events, onSubmitted }: Props) {
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<PendingMessage[]>([]);
  const [composerError, setComposerError] = useState<string | null>(null);
  const [commands, setCommands] = useState<CommandMetadata[]>([]);
  const [claudeCommands, setClaudeCommands] = useState<ClaudeCommandMetadata[]>([]);
  const [commandResult, setCommandResult] = useState<CommandResult | null>(null);
  const [commandResultNamespace, setCommandResultNamespace] = useState<"platform" | "claude">("platform");
  const [commandBusy, setCommandBusy] = useState(false);
  const [menuDismissed, setMenuDismissed] = useState(false);
  const [activeCommand, setActiveCommand] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  // Drop pending entries once the durable conversation contains them.
  useEffect(() => {
    if (!messages) return;
    const known = new Set(messages.map((message) => message.client_message_id).filter(Boolean));
    setPending((current) => current.filter((item) => !known.has(item.clientMessageId)));
  }, [messages]);

  // Reset per-task UI state when switching tasks.
  useEffect(() => {
    setDraft("");
    setPending([]);
    setComposerError(null);
    setCommandResult(null);
    setCommands([]);
    setClaudeCommands([]);
    setMenuDismissed(false);
    stickToBottom.current = true;
  }, [detail.id]);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      api.getCommands(detail.id, controller.signal),
      api.getClaudeCommands(detail.id, controller.signal),
    ])
      .then(([platform, claude]) => {
        setCommands(platform);
        setClaudeCommands(claude);
      })
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) {
          setComposerError(reason instanceof Error ? reason.message : String(reason));
        }
      });
    return () => controller.abort();
  }, [detail.id, detail.status, detail.workflow_phase, detail.pause_requested, detail.writer]);

  useLayoutEffect(() => {
    const list = listRef.current;
    if (list && stickToBottom.current) list.scrollTop = list.scrollHeight;
  }, [messages, pending, events, commandResult]);

  const running = messages?.some((message) => message.status === "RUNNING") ?? false;
  const working = detail.agent_working || running;
  const maxLength = config?.max_message_length ?? 8000;
  // The backend decides (role + task state); the UI only renders its reason.
  const disabledReason = !config
    ? "Loading…"
    : !detail.messaging.accepting
      ? (detail.messaging.reason ?? "This task cannot receive messages in its current state.")
      : null;
  const trimmedDraft = draft.trim();
  const isPlatformCommand = trimmedDraft.startsWith("/");
  const isClaudeCommand = trimmedDraft.toLowerCase().startsWith("claude/");
  const isCommand = isPlatformCommand || isClaudeCommand;
  const commandPrefix = trimmedDraft.split(/\s/, 1)[0].toLowerCase();
  const matchingCommands = isClaudeCommand
    ? claudeCommands.filter((command) => command.command.toLowerCase().startsWith(commandPrefix))
    : commands.filter((command) => command.name.toLowerCase().startsWith(commandPrefix));
  const showCommandMenu =
    !menuDismissed && isCommand && !draft.trimStart().includes(" ");

  async function send(text: string, clientMessageId: string) {
    setComposerError(null);
    setPending((current) => [
      ...current.filter((item) => item.clientMessageId !== clientMessageId),
      { clientMessageId, text, state: "sending" },
    ]);
    try {
      await api.postMessage(detail.id, { message: text, client_message_id: clientMessageId });
      setPending((current) =>
        current.map((item) =>
          item.clientMessageId === clientMessageId ? { ...item, state: "accepted" } : item,
        ),
      );
      onSubmitted();
    } catch (reason) {
      const error = reason instanceof ApiError ? reason : new ApiError(String(reason), 0);
      if (error.retryable) {
        // Keep it with the same client_message_id: a retry can never duplicate it.
        setPending((current) =>
          current.map((item) =>
            item.clientMessageId === clientMessageId
              ? { ...item, state: "failed", error: error.message }
              : item,
          ),
        );
      } else {
        // Rejected by the server (409/422/503): nothing was recorded; give the text back.
        setPending((current) => current.filter((item) => item.clientMessageId !== clientMessageId));
        setDraft((current) => current || text);
        setComposerError(error.message);
      }
    }
  }

  async function execute(text: string) {
    setComposerError(null);
    setCommandResult(null);
    setCommandBusy(true);
    try {
      const claude = text.trimStart().toLowerCase().startsWith("claude/");
      const result = claude
        ? await api.executeClaudeCommand(detail.id, text, newClientId())
        : await api.executeCommand(detail.id, text, newClientId());
      setCommandResult(result);
      setCommandResultNamespace(claude ? "claude" : "platform");
      setDraft("");
      setMenuDismissed(false);
      onSubmitted();
    } catch (reason) {
      const error = reason instanceof ApiError ? reason : new ApiError(String(reason), 0);
      setComposerError(error.message);
      onSubmitted(); // failed command attempts are durable platform events too
    } finally {
      setCommandBusy(false);
    }
  }

  function submit() {
    const text = draft.trim();
    if (!text || text.length > maxLength) return;
    if (text.startsWith("/") || text.toLowerCase().startsWith("claude/")) {
      void execute(text);
      return;
    }
    if (disabledReason) return;
    setDraft("");
    stickToBottom.current = true;
    void send(text, newClientId());
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (showCommandMenu && matchingCommands.length > 0) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const delta = event.key === "ArrowDown" ? 1 : -1;
        setActiveCommand((current) =>
          (current + delta + matchingCommands.length) % matchingCommands.length,
        );
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        setMenuDismissed(true);
        return;
      }
      if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
        event.preventDefault();
        setDraft(matchingCommands[Math.min(activeCommand, matchingCommands.length - 1)].usage);
        setMenuDismissed(true);
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <div className="chat">
      <div
        className="chat-log"
        ref={listRef}
        onScroll={(event) => {
          const list = event.currentTarget;
          stickToBottom.current = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
        }}
      >
        {messages === null && <div className="empty-state">Loading conversation…</div>}
        {messages?.length === 0 && pending.length === 0 && (
          <div className="empty-state">No messages in this task's conversation yet.</div>
        )}
        {messages?.map((message, index) => {
          const answered =
            message.type === "human_input_required" &&
            messages
              .slice(index + 1)
              .some((later) => later.type === "human_message" && later.sequence_id > message.sequence_id);
          return (
            <ChatEntry
              message={message}
              own={message.actor_id === user.username}
              answered={answered}
              key={message.id}
            />
          );
        })}
        {pending.map((item) => (
          <article className="message human own pending" key={item.clientMessageId}>
            <div className="event-heading">
              <strong>
                {user.display_name}
              </strong>
              <time>{item.state === "failed" ? "not sent" : "sending…"}</time>
            </div>
            <p>{item.text}</p>
            {item.state === "failed" && (
              <span className="delivery delivery-failed">
                {item.error}{" "}
                <button className="link-button" onClick={() => void send(item.text, item.clientMessageId)}>
                  Retry
                </button>
              </span>
            )}
          </article>
        ))}
        {working && (
          <div className="working" role="status">
            <span className="spinner" aria-hidden="true" /> Agent is working…
            {detail.queued_messages > 0 && ` · ${detail.queued_messages} queued`}
          </div>
        )}
        {!working && detail.queued_messages > 0 && (
          <div className="working idle" role="status">
            {detail.queued_messages} message(s) queued
            {config && !config.runner_enabled && " — the message runner is not enabled on this server"}
          </div>
        )}
      </div>

      <div className="composer">
        {detail.status === "WAITING_FOR_HUMAN" && (
            <div className="input-required-banner"><span aria-hidden="true">!</span><div><strong>Claude needs your input to continue {detail.workflow_phase.replaceAll("_", " ").toLowerCase()}.</strong><small>Answer below and the same phase will resume automatically.</small></div></div>
        )}
        {commandResult && (
          <div className="command-result" role="status">
            <strong>{commandResultNamespace === "claude" ? "Claude command" : "Platform"}</strong>{" "}
            {commandResult.message}
          </div>
        )}
        {composerError && <div className="composer-error">{composerError}</div>}
        {showCommandMenu && (
          <div
            className={`command-menu ${isClaudeCommand ? "claude" : "platform"}`}
            role="listbox"
            aria-label={isClaudeCommand ? "Claude commands" : "Platform commands"}
          >
            <div className="command-namespace">{isClaudeCommand ? "Claude" : "Platform"}</div>
            {matchingCommands.length === 0 ? (
              <div className="command-empty">
                No matching {isClaudeCommand ? "Claude" : "platform"} command
              </div>
            ) : (
              matchingCommands.map((command, index) => (
                <button
                  type="button"
                  role="option"
                  aria-selected={index === activeCommand}
                  className={`${index === activeCommand ? "active" : ""} ${command.available ? "" : "disabled"}`}
                  key={"command" in command ? command.command : command.name}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    setDraft(command.usage);
                    setMenuDismissed(true);
                  }}
                >
                  <span>
                    <strong>{"command" in command ? command.command : command.name}</strong>{" "}
                    {command.description}
                    {"classification" in command && (
                      <small className="classification"> {command.classification}</small>
                    )}
                  </span>
                  {!command.available && <small>{command.disabled_reason}</small>}
                </button>
              ))
            )}
          </div>
        )}
        <textarea
          value={draft}
          onChange={(event) => {
            setDraft(event.target.value);
            setMenuDismissed(false);
            setActiveCommand(0);
          }}
          onKeyDown={onKeyDown}
          placeholder={
            disabledReason ??
            detail.status === "WAITING_FOR_HUMAN"
              ? "Answer this question..."
              : "Ask or instruct the task..."
          }
          disabled={commandBusy}
          rows={3}
          maxLength={maxLength}
          aria-label="Message"
        />
        <div className="composer-footer">
          <span className="muted">
            {isCommand
              ? isClaudeCommand
                ? "Claude commands are allowlisted and audited; no shell passthrough."
                : "Platform commands are deterministic and are not sent to Claude."
              : disabledReason
              ? "Read-only"
              : working
                ? "Claude is busy; your message will be queued."
                : `Enter to send · Shift+Enter for a new line · / commands · claude/ capabilities`}
          </span>
          <button
            className="send"
            onClick={submit}
            disabled={commandBusy || !draft.trim() || (!isCommand && disabledReason !== null)}
          >
            {commandBusy ? "Running…" : isCommand ? "Run command" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}
