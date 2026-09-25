// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TaskDetail } from "../types/api";
import { TaskControls } from "./TaskControls";

const mocks = vi.hoisted(() => ({
  control: vi.fn(),
  removeTask: vi.fn(),
}));

vi.mock("../api/client", () => ({
  ApiError: class ApiError extends Error {
    status = 0;
    retryable = false;
  },
  api: mocks,
}));

const detail = {
  id: "DEMO-1",
  title: "Task",
  difficulty: "LOW",
  status: "COMPLETED",
  workflow_phase: "HUMAN_REVIEW",
  model_tier: null,
  writer: null,
  updated_at: "2026-01-01T00:00:00Z",
  description: "Task",
  acceptance_criteria: ["Done"],
  model_name: null,
  workspace_id: "DEMO-1",
  current_attempt: 1,
  verification_status: "PASSED",
  verification_result: null,
  agent_working: false,
  pause_requested: false,
  queued_messages: 0,
  messaging: { accepting: false, reason: "Completed" },
  actions: Object.fromEntries(
    ["start", "pause", "resume", "approve", "defer", "reject", "reset"].map((name) => [
      name,
      { allowed: name === "reset", reason: name === "reset" ? null : "Unavailable" },
    ]),
  ),
  created_at: "2026-01-01T00:00:00Z",
  default_model_selection: "AUTO",
  latest_readiness: null,
  disposition: "CONFIRMED",
  can_remove: true,
  remove_disabled_reason: null,
  repository_id: null,
  base_branch: null,
} as TaskDetail;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("permanent task removal", () => {
  it("requires the destructive confirmation and reports successful removal", async () => {
    mocks.removeTask.mockResolvedValue(undefined);
    const onRemoved = vi.fn();
    render(<TaskControls detail={detail} onChanged={vi.fn()} onRemoved={onRemoved} />);

    fireEvent.click(screen.getByRole("button", { name: "Remove task…" }));
    expect(mocks.removeTask).not.toHaveBeenCalled();
    expect(screen.getByText("This cannot be undone.")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Remove permanently" }));
    await waitFor(() => expect(mocks.removeTask).toHaveBeenCalledWith("DEMO-1"));
    expect(onRemoved).toHaveBeenCalledOnce();
  });

  it("does not render removal when backend availability is false", () => {
    render(
      <TaskControls
        detail={{ ...detail, can_remove: false, remove_disabled_reason: "Not terminal" }}
        onChanged={vi.fn()}
        onRemoved={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Remove task…" })).toBeNull();
  });
});
