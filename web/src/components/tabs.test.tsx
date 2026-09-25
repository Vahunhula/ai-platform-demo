// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { PlatformEvent } from "../types/api";
import { TraceTab } from "./tabs";

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
    render(<TraceTab events={events} />);

    const sequenceLabels = screen.getAllByText(/^#\d+$/).map((node) => node.textContent);
    expect(sequenceLabels).toEqual(["#3", "#2", "#1"]);
  });

  it("switches to oldest-first when toggled", () => {
    const events = [event(1), event(2), event(3)];
    render(<TraceTab events={events} />);

    fireEvent.click(screen.getByText(/switch/));

    const sequenceLabels = screen.getAllByText(/^#\d+$/).map((node) => node.textContent);
    expect(sequenceLabels).toEqual(["#1", "#2", "#3"]);
  });

  it("paginates: newly arrived events land first and older ones load on demand", () => {
    const many = Array.from({ length: 45 }, (_, index) => event(index + 1));
    const { rerender } = render(<TraceTab events={many} />);

    // Newest-first, first page only (40 of 45): #45 is visible, #1 is not yet.
    expect(screen.getByText("#45")).toBeTruthy();
    expect(screen.queryByText("#1")).toBeNull();

    fireEvent.click(screen.getByText(/Load \d+ older events/));
    expect(screen.getByText("#1")).toBeTruthy();

    // A live SSE arrival (#46) becomes the new first item without a reload.
    rerender(<TraceTab events={[...many, event(46)]} />);
    const sequenceLabels = screen.getAllByText(/^#\d+$/).map((node) => node.textContent);
    expect(sequenceLabels[0]).toBe("#46");
  });

  it("expands and collapses one event's sanitized metadata", () => {
    const withMetadata: PlatformEvent = { ...event(1), metadata: { tier: "default" } };
    render(<TraceTab events={[withMetadata]} />);

    expect(screen.queryByText(/"tier"/)).toBeNull();
    fireEvent.click(screen.getByText("Details"));
    expect(screen.getByText(/"tier"/)).toBeTruthy();
    fireEvent.click(screen.getByText("Hide details"));
    expect(screen.queryByText(/"tier"/)).toBeNull();
  });
});
