// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PlatformEvent } from "../types/api";
import { ActivityTab } from "./tabs";

function event(sequenceId: number, type = "STATUS_CHANGED"): PlatformEvent {
  return {
    sequence_id: sequenceId,
    timestamp: `2026-01-01T00:00:${String(sequenceId).padStart(2, "0")}Z`,
    event_type: type,
    actor_type: "system",
    actor_id: "task-graph",
    actor_display_name: "Platform",
    execution_id: null,
    metadata: {},
  };
}

afterEach(() => cleanup());

describe("Activity tab ordering (Phase 6)", () => {
  it("shows the newest event first by default, without altering sequence numbers", () => {
    const events = [event(1), event(2), event(3)];
    render(<ActivityTab events={events} />);

    const sequenceLabels = screen.getAllByText(/^#\d+$/).map((node) => node.textContent);
    expect(sequenceLabels).toEqual(["#3", "#2", "#1"]);
  });

  it("requests an older server page without client-side full-history pagination", () => {
    const onLoadOlder = vi.fn();
    render(<ActivityTab events={[event(45), event(44)]} hasOlder onLoadOlder={onLoadOlder} />);
    fireEvent.click(screen.getByRole("button", { name: "Load older events" }));
    expect(onLoadOlder).toHaveBeenCalledOnce();
  });

  it("expands and collapses one event's sanitized metadata", () => {
    const withMetadata: PlatformEvent = { ...event(1), metadata: { tier: "default" } };
    render(<ActivityTab events={[withMetadata]} />);

    expect(screen.queryByText(/"tier"/)).toBeNull();
    fireEvent.click(screen.getByText("Details"));
    expect(screen.getByText(/"tier"/)).toBeTruthy();
    fireEvent.click(screen.getByText("Hide details"));
    expect(screen.queryByText(/"tier"/)).toBeNull();
  });
});
