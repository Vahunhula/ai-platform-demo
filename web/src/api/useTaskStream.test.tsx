// @vitest-environment jsdom
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useTaskStream } from "./useTaskStream";

const sources: FakeEventSource[] = [];
class FakeEventSource {
  static CLOSED = 2;
  readyState = 1;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, (event: Event) => void>();
  constructor(readonly url: string) { sources.push(this); }
  addEventListener(name: string, callback: (event: Event) => void) { this.listeners.set(name, callback); }
  close() { this.readyState = FakeEventSource.CLOSED; }
  emit(name: string, data = "") { this.listeners.get(name)?.({ data } as unknown as Event); }
}
vi.mock("./client", () => ({ api: { streamUrl: (id: string, after: number) => `/api/tasks/${id}/stream?after=${after}` } }));
afterEach(() => { cleanup(); sources.length = 0; });
it("delivers new SSE task updates once and reports a live connection", async () => {
  Object.defineProperty(globalThis, "EventSource", { value: FakeEventSource, configurable: true });
  const onEvent = vi.fn();
  const { result } = renderHook(() => useTaskStream("TASK-1", 4, { onEvent, onConversation: vi.fn() }));
  sources[0].onopen?.();
  await waitFor(() => expect(result.current).toBe("live"));
  const event = { sequence_id: 5, timestamp:"",event_type:"STATUS_CHANGED",actor_type:"system",actor_id:"platform",actor_display_name:"Platform",execution_id:null,metadata:{} };
  sources[0].emit("platform_event", JSON.stringify(event));
  sources[0].emit("platform_event", JSON.stringify(event));
  expect(onEvent).toHaveBeenCalledOnce();
});
