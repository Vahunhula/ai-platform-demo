import { type KeyboardEvent, useEffect, useLayoutEffect, useRef, useState } from "react";

import { ApiError, api } from "../api/client";
import { newClientId } from "../api/ids";
import type { ConfigResponse, ConversationMessage, TaskDetail } from "../types/api";
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
  messages: ConversationMessage[] | null;
  onSubmitted: () => void;
}

export function ChatPanel({ detail, config, messages, onSubmitted }: Props) {
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<PendingMessage[]>([]);
  const [composerError, setComposerError] = useState<string | null>(null);
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
    stickToBottom.current = true;
  }, [detail.id]);

  useLayoutEffect(() => {
    const list = listRef.current;
    if (list && stickToBottom.current) list.scrollTop = list.scrollHeight;
  }, [messages, pending]);

  const running = messages?.some((message) => message.status === "RUNNING") ?? false;
  const working = detail.agent_working || running;
  const maxLength = config?.max_message_length ?? 8000;
  const disabledReason = !config
    ? "Loading…"
    : !config.messaging_enabled
      ? "Browser messaging is disabled on this server (AI_PLATFORM_WEB_ACTOR is not set)."
      : !detail.messaging.accepting
        ? (detail.messaging.reason ?? "This task cannot receive messages in its current state.")
        : null;

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

  function submit() {
    const text = draft.trim();
    if (!text || disabledReason || text.length > maxLength) return;
    setDraft("");
    stickToBottom.current = true;
    void send(text, newClientId());
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
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
        {messages?.map((message) => (
          <article className={`message ${message.role}`} key={message.id}>
            <div className="event-heading">
              <strong>
                {message.actor_id} <span className="muted">({message.role})</span>
              </strong>
              <time>
                #{message.sequence_id} · {formatTime(message.timestamp)}
              </time>
            </div>
            <p>{message.content}</p>
            {message.status && message.status !== "COMPLETED" && (
              <span className={`delivery delivery-${message.status.toLowerCase()}`}>
                {STATUS_LABEL[message.status]}
                {message.error && `: ${message.error}`}
              </span>
            )}
          </article>
        ))}
        {pending.map((item) => (
          <article className="message human pending" key={item.clientMessageId}>
            <div className="event-heading">
              <strong>
                {config?.web_actor ?? "you"} <span className="muted">(human)</span>
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
        {composerError && <div className="composer-error">{composerError}</div>}
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder={
            disabledReason ?? `Message ${detail.id} as ${config?.web_actor} (Enter to send, Shift+Enter for a new line)`
          }
          disabled={disabledReason !== null}
          rows={3}
          maxLength={maxLength}
          aria-label="Message"
        />
        <div className="composer-footer">
          <span className="muted">
            {disabledReason
              ? "Read-only"
              : working
                ? "Claude is busy; your message will be queued."
                : `Posting as ${config?.web_actor} (server-configured, not authenticated)`}
          </span>
          <button
            className="send"
            onClick={submit}
            disabled={disabledReason !== null || !draft.trim()}
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}
