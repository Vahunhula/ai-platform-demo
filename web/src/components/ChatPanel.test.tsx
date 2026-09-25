// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  CommandMetadata,
  ConfigResponse,
  CurrentUser,
  TaskDetail,
} from "../types/api";
import { ChatPanel } from "./ChatPanel";

const mocks = vi.hoisted(() => ({
  getCommands: vi.fn(),
  getClaudeCommands: vi.fn(),
  executeCommand: vi.fn(),
  executeClaudeCommand: vi.fn(),
  postMessage: vi.fn(),
}));

vi.mock("../api/client", () => ({
  ApiError: class ApiError extends Error {
    status = 0;
    retryable = false;
  },
  api: mocks,
}));

vi.mock("../api/ids", () => ({ newClientId: () => "browser-command-0001" }));

const user: CurrentUser = {
  id: "u1",
  username: "dev",
  display_name: "Dev",
  role: "developer",
  can_modify_tasks: true,
};

const config: ConfigResponse = {
  runner_enabled: true,
  max_message_length: 8000,
  presence_heartbeat_seconds: 15,
};

const detail = {
  id: "DEMO-1",
  title: "Task",
  difficulty: "LOW",
  status: "READY",
  workflow_phase: "IMPLEMENTATION",
  model_tier: null,
  writer: null,
  updated_at: "2026-01-01T00:00:00Z",
  description: "Task",
  acceptance_criteria: ["Done"],
  model_name: null,
  workspace_id: null,
  current_attempt: 0,
  verification_status: "NOT_RUN",
  verification_result: null,
  agent_working: false,
  pause_requested: false,
  queued_messages: 0,
  messaging: { accepting: true, reason: null },
  actions: Object.fromEntries(
    ["start", "pause", "resume", "approve", "defer", "reject", "reset"].map((name) => [
      name,
      { allowed: false, reason: "Unavailable" },
    ]),
  ),
  created_at: "2026-01-01T00:00:00Z",
  default_model_selection: "AUTO",
  latest_readiness: null,
  disposition: null,
  can_remove: false,
  remove_disabled_reason: "Only terminal tasks can be removed.",
} as TaskDetail;

const command = (
  name: string,
  available = true,
  disabledReason: string | null = null,
): CommandMetadata => ({
  name,
  description: `Description for ${name}`,
  usage: name === "/reject" ? "/reject <feedback>" : name,
  arguments: name === "/reject" ? ["feedback (required)"] : [],
  required_permission: name === "/status" ? "viewer" : "developer",
  available,
  disabled_reason: disabledReason,
  mutating: name !== "/status",
});

function show() {
  return render(
    <ChatPanel
      detail={detail}
      config={config}
      user={user}
      messages={[]}
      events={[]}
      onSubmitted={vi.fn()}
    />,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("platform command composer", () => {
  it("opens for slash prefixes, filters, shows disabled reasons, and selects with Enter", async () => {
    mocks.getClaudeCommands.mockResolvedValue([]);
    mocks.getCommands.mockResolvedValue([
      command("/review"),
      command("/reject", false, "Only while waiting for review."),
      command("/resume"),
      command("/status"),
    ]);
    show();
    const composer = screen.getByLabelText("Message") as HTMLTextAreaElement;

    fireEvent.change(composer, { target: { value: "/re" } });

    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(3));
    expect(screen.getByText("Only while waiting for review.")).toBeTruthy();
    fireEvent.keyDown(composer, { key: "Enter" });
    expect(composer.value).toBe("/review");
  });

  it("keeps ordinary text on the message API and sends leading slash text to command API", async () => {
    mocks.getClaudeCommands.mockResolvedValue([]);
    mocks.getCommands.mockResolvedValue([command("/status")]);
    mocks.postMessage.mockResolvedValue({});
    mocks.executeCommand.mockResolvedValue({
      command: "/status",
      status: "completed",
      message: "Phase: IMPLEMENTATION",
      data: {},
    });
    show();
    const composer = screen.getByLabelText("Message");

    fireEvent.change(composer, { target: { value: "Please inspect /status handling" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    await waitFor(() => expect(mocks.postMessage).toHaveBeenCalledOnce());
    expect(mocks.executeCommand).not.toHaveBeenCalled();

    fireEvent.change(composer, { target: { value: " /status " } });
    fireEvent.click(screen.getByRole("button", { name: "Run command" }));
    await waitFor(() => expect(mocks.executeCommand).toHaveBeenCalledOnce());
    expect(mocks.executeCommand.mock.calls[0][1]).toBe("/status");
  });

  it("opens a distinct Claude catalog, filters it, and never treats mentions as commands", async () => {
    mocks.getCommands.mockResolvedValue([command("/status")]);
    mocks.getClaudeCommands.mockResolvedValue([
      {
        namespace: "claude",
        command: "claude/help",
        description: "Audited help",
        usage: "claude/help",
        classification: "ADAPTED",
        required_permission: "viewer",
        executor_capability: "registry_metadata",
        available: true,
        disabled_reason: null,
      },
      {
        namespace: "claude",
        command: "claude/status",
        description: "Safe status",
        usage: "claude/status",
        classification: "ADAPTED",
        required_permission: "viewer",
        executor_capability: "platform_executor_status",
        available: true,
        disabled_reason: null,
      },
    ]);
    mocks.postMessage.mockResolvedValue({});
    mocks.executeClaudeCommand.mockResolvedValue({
      command: "claude/status",
      status: "completed",
      message: "Claude executor: claude",
      data: {},
    });
    show();
    const composer = screen.getByLabelText("Message");

    fireEvent.change(composer, { target: { value: "claude/st" } });
    await waitFor(() => expect(screen.getByRole("listbox", { name: "Claude commands" })).toBeTruthy());
    expect(screen.getAllByRole("option")).toHaveLength(1);
    fireEvent.keyDown(composer, { key: "Enter" });
    expect((composer as HTMLTextAreaElement).value).toBe("claude/status");
    fireEvent.keyDown(composer, { key: "Enter" });
    await waitFor(() => expect(mocks.executeClaudeCommand).toHaveBeenCalledOnce());

    fireEvent.change(composer, { target: { value: "Please inspect claude/config.py" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    await waitFor(() => expect(mocks.postMessage).toHaveBeenCalledOnce());
  });
});
