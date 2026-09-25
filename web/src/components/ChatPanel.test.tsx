// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  CommandMetadata,
  ConfigResponse,
  ConversationMessage,
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
  repository_id: null,
  base_branch: null,
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

function show(messages: ConversationMessage[] = []) {
  return render(
    <ChatPanel
      detail={detail}
      config={config}
      user={user}
      messages={messages}
      events={[]}
      onSubmitted={vi.fn()}
    />,
  );
}

const baseItem: ConversationMessage = {
  id: "item-1",
  task_id: "DEMO-1",
  type: "platform_activity",
  role: "platform",
  actor_id: "task-graph",
  actor_display_name: "Platform",
  title: null,
  content: "",
  timestamp: "2026-01-01T00:00:00Z",
  sequence_id: 1,
  turn_id: null,
  workflow_phase: null,
  artifact_kind: null,
  artifact_version: null,
  readiness_score: null,
  requires_human_input: false,
  blocking_checks: null,
  logical_model: null,
  concrete_model: null,
  status: null,
  error: null,
  client_message_id: null,
  channel: null,
};

function chatItem(overrides: Partial<ConversationMessage>): ConversationMessage {
  return { ...baseItem, ...overrides };
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

describe("Chat as a projection of workflow state (Phase 6)", () => {
  it("shows a phase's actual output directly in Chat, not just a phase-changed line", async () => {
    mocks.getClaudeCommands.mockResolvedValue([]);
    mocks.getCommands.mockResolvedValue([]);
    show([
      chatItem({
        id: "plan-1",
        type: "phase_result",
        role: "agent",
        actor_id: "agent:plan",
        actor_display_name: "Claude",
        title: "Claude · Plan",
        content: "Implement the described change.\n\nSteps\n- Make the change.\n- Run the tests.",
        sequence_id: 5,
        workflow_phase: "PLAN",
        artifact_kind: "PLAN",
        artifact_version: 1,
        logical_model: "CLAUDE_SONNET",
      }),
    ]);

    expect(screen.getByText("Claude · Plan")).toBeTruthy();
    expect(screen.getByText(/Implement the described change\./)).toBeTruthy();
    expect(screen.getByText(/Run the tests\./)).toBeTruthy();
    expect(screen.getByText("v1")).toBeTruthy();
  });

  it("shows a waiting-for-human question and its blocking checks directly in Chat", async () => {
    mocks.getClaudeCommands.mockResolvedValue([]);
    mocks.getCommands.mockResolvedValue([]);
    show([
      chatItem({
        id: "needs-input-1",
        type: "human_input_required",
        role: "platform",
        actor_display_name: "Platform",
        title: "Needs your input · Plan",
        content: "The Plan phase cannot proceed automatically.\n\nQuestions:\n1. Open questions are resolved",
        sequence_id: 6,
        workflow_phase: "PLAN",
        readiness_score: 82,
        requires_human_input: true,
        blocking_checks: [
          { key: "open_questions_resolved", label: "Open questions are resolved", status: "NEEDS_HUMAN", evidence: "1 open question(s) block implementation" },
        ],
      }),
    ]);

    expect(screen.getByText("Needs your input · Plan")).toBeTruthy();
    expect(screen.getByText(/cannot proceed automatically/)).toBeTruthy();
    expect(screen.getByText("Open questions are resolved")).toBeTruthy();
    expect(screen.getByText("Needs human")).toBeTruthy();
    expect(screen.getByText("Needs your input")).toBeTruthy();
  });

  it("renders platform activity (phase started / gate passed) as a narrow status line", async () => {
    mocks.getClaudeCommands.mockResolvedValue([]);
    mocks.getCommands.mockResolvedValue([]);
    show([
      chatItem({
        id: "activity-1",
        type: "platform_activity",
        content: "Plan phase started",
        sequence_id: 3,
      }),
    ]);

    expect(screen.getByText("Plan phase started")).toBeTruthy();
  });

  it("renders human, agent, and command_result items in one chronologically ordered list", async () => {
    mocks.getClaudeCommands.mockResolvedValue([]);
    mocks.getCommands.mockResolvedValue([]);
    show([
      chatItem({ id: "h1", type: "human_message", role: "human", actor_id: "dev", actor_display_name: "Dev", content: "/plan", sequence_id: 1 }),
      chatItem({ id: "c1", type: "command_result", role: "platform", actor_display_name: "Platform", content: "Workflow moved to PLAN.", sequence_id: 2 }),
      chatItem({ id: "a1", type: "agent_message", role: "agent", actor_id: "claude", actor_display_name: "Claude", content: "Working on it.", sequence_id: 3 }),
    ]);

    const log = screen.getByText("Working on it.").closest(".chat-log") as HTMLElement;
    const texts = [...log.querySelectorAll("article p")].map((node) => node.textContent);
    expect(texts).toEqual(["/plan", "Workflow moved to PLAN.", "Working on it."]);
  });
});
