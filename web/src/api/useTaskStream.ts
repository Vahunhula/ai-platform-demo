import { useEffect, useRef, useState } from "react";

import type { PlatformEvent } from "../types/api";
import { api } from "./client";

export type StreamState = "idle" | "connecting" | "live" | "reconnecting";

interface Handlers {
  onEvent: (event: PlatformEvent) => void;
  onConversation: () => void;
}

const MAX_BACKOFF_MS = 15_000;
// The server sends a heartbeat every 10 s when idle. Hearing nothing for longer
// than this means the connection is dead even if a proxy is holding it open.
const SILENCE_TIMEOUT_MS = 25_000;

/**
 * Subscribe to one task's SSE stream starting after `afterSequence`.
 *
 * `afterSequence === null` means "not ready" (history not loaded yet).
 *
 * Reconnection: for a dropped connection, EventSource retries by itself and sends
 * Last-Event-ID, which the server prefers over `?after`. But if a retry receives an
 * HTTP error (e.g. a proxy answering 502 while the API restarts), EventSource gives
 * up for good; and some proxies keep a dead connection open without any error. The
 * hook therefore also watches for silence (no event or heartbeat) and in both cases
 * reopens the stream itself, with backoff, from the last sequence it delivered, so
 * no event is skipped or delivered twice.
 */
export function useTaskStream(
  taskId: string | null,
  afterSequence: number | null,
  handlers: Handlers,
): StreamState {
  const [state, setState] = useState<StreamState>("idle");
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    if (!taskId || afterSequence === null) {
      setState("idle");
      return;
    }
    const id = taskId;
    let cursor = afterSequence;
    let source: EventSource | null = null;
    let retryTimer: number | undefined;
    let silenceTimer: number | undefined;
    let attempt = 0;
    let disposed = false;

    const reopenLater = () => {
      source?.close();
      window.clearTimeout(silenceTimer);
      attempt += 1;
      setState("reconnecting");
      const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** Math.min(attempt - 1, 4));
      retryTimer = window.setTimeout(connect, delay);
    };

    const heardFromServer = () => {
      window.clearTimeout(silenceTimer);
      silenceTimer = window.setTimeout(reopenLater, SILENCE_TIMEOUT_MS);
    };

    function connect() {
      if (disposed) return;
      setState(attempt === 0 ? "connecting" : "reconnecting");
      source = new EventSource(api.streamUrl(id, cursor));
      heardFromServer();
      source.onopen = () => {
        attempt = 0;
        setState("live");
        heardFromServer();
      };
      source.onerror = () => {
        if (source?.readyState === EventSource.CLOSED) reopenLater();
        else setState("reconnecting"); // the browser retries with Last-Event-ID
      };
      source.addEventListener("platform_event", (message) => {
        heardFromServer();
        const event = JSON.parse((message as MessageEvent<string>).data) as PlatformEvent;
        if (event.sequence_id <= cursor) return; // never deliver an event twice
        cursor = event.sequence_id;
        handlersRef.current.onEvent(event);
      });
      source.addEventListener("conversation", () => {
        heardFromServer();
        handlersRef.current.onConversation();
      });
      source.addEventListener("heartbeat", heardFromServer);
    }

    connect();
    return () => {
      disposed = true;
      window.clearTimeout(retryTimer);
      window.clearTimeout(silenceTimer);
      source?.close();
    };
    // Only (re)connect when the task changes or its history (re)loads; later events
    // advance the cursor without reconnecting.
  }, [taskId, afterSequence === null]);

  return state;
}
