// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { PlatformEvent, TaskDetail } from "../types/api";
import { WorkflowProgress } from "./WorkflowProgress";

const baseDetail = {
  id: "DEMO-1",
  title: "Task",
  difficulty: "LOW",
  status: "WAITING_FOR_HUMAN",
  workflow_phase: "PLAN",
  model_tier: null,
  writer: null,
  updated_at: "2026-01-01T00:00:00Z",
  description: "Task",
  acceptance_criteria: [] as string[],
  model_name: null,
  workspace_id: null,
  current_attempt: 1,
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
  latest_readiness: { phase: "PLAN", score: 82, eligible_for_auto_progression: false },
  disposition: null,
  can_remove: false,
  remove_disabled_reason: null,
  repository_id: null,
  base_branch: null,
} as TaskDetail;

function phaseEvent(phase: string): PlatformEvent {
  return {
    sequence_id: 1,
    timestamp: "2026-01-01T00:00:00Z",
    event_type: "WORKFLOW_PHASE_STARTED",
    actor_type: "system",
    actor_id: "task-graph",
    actor_display_name: "Platform",
    execution_id: null,
    metadata: { phase },
  };
}

afterEach(() => cleanup());

describe("Workflow progress visualization (Phase 6)", () => {
  it("marks earlier phases done, the current phase waiting, and later phases upcoming", () => {
    render(<WorkflowProgress detail={baseDetail} events={[phaseEvent("BRAINSTORM"), phaseEvent("PLAN")]} />);

    const steps = screen.getAllByRole("listitem");
    expect(steps).toHaveLength(5);
    expect(steps[0].querySelector(".workflow-dot")?.className).toContain("workflow-done"); // Brainstorm
    expect(steps[1].querySelector(".workflow-dot")?.className).toContain("workflow-waiting"); // Plan (current, waiting)
    expect(steps[2].querySelector(".workflow-dot")?.className).toContain("workflow-upcoming"); // Implementation
    expect(screen.getByText("82%")).toBeTruthy();
  });

  it("shows the current phase as plain 'current' (not waiting) when not blocked", () => {
    const running = { ...baseDetail, status: "IMPLEMENTING", workflow_phase: "IMPLEMENTATION" } as TaskDetail;
    render(<WorkflowProgress detail={running} events={[]} />);

    const steps = screen.getAllByRole("listitem");
    expect(steps[2].querySelector(".workflow-dot")?.className).toContain("workflow-current");
  });
});
