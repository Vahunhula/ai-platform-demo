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

  it("keeps removal discoverable and explains backend unavailability", () => {
    render(
      <TaskControls
        detail={{ ...detail, can_remove: false, remove_disabled_reason: "Not terminal" }}
        onChanged={vi.fn()}
        onRemoved={vi.fn()}
      />,
    );
    expect((screen.getByRole("button", { name: "Remove task…" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Disabled — Not terminal")).toBeTruthy();
  });

  it("shows Confirm and Defer in Human Review while removal remains disabled", () => {
    const actions = { ...detail.actions };
    actions.approve = { allowed: true, reason: null };
    actions.defer = { allowed: true, reason: null };
    render(
      <TaskControls
        detail={{
          ...detail,
          status: "WAITING_FOR_HUMAN",
          disposition: null,
          can_remove: false,
          remove_disabled_reason: "Confirm or Defer the task in Human Review first.",
          actions,
        }}
        onChanged={vi.fn()}
        onRemoved={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Confirm" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Defer…" })).toBeTruthy();
    expect((screen.getByRole("button", { name: "Remove task…" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/Confirm or Defer the task/)).toBeTruthy();
  });

  it.each(["CONFIRMED", "DEFERRED"] as const)(
    "enables removal for a terminal %s task",
    (disposition) => {
      render(
        <TaskControls
          detail={{ ...detail, disposition, can_remove: true, remove_disabled_reason: null }}
          onChanged={vi.fn()}
          onRemoved={vi.fn()}
        />,
      );
      expect((screen.getByRole("button", { name: "Remove task…" }) as HTMLButtonElement).disabled).toBe(false);
    },
  );

  it("cancels permanent removal without calling DELETE", () => {
    render(<TaskControls detail={detail} onChanged={vi.fn()} onRemoved={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Remove task…" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mocks.removeTask).not.toHaveBeenCalled();
  });

  it("shows a read-only reason for viewers and never opens the dialog", () => {
    render(
      <TaskControls
        detail={{
          ...detail,
          can_remove: false,
          remove_disabled_reason: "Developer access is required to remove a task.",
        }}
        onChanged={vi.fn()}
        onRemoved={vi.fn()}
      />,
    );
    const button = screen.getByRole("button", { name: "Remove task…" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
