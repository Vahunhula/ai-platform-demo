// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TestsTab } from "./tabs";

const getTaskTests = vi.hoisted(() => vi.fn());
vi.mock("../api/client", () => ({ api: { getTaskTests } }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const response = {
  task_id: "TASK-1",
  human_requirements: "✓ Quantity 10 should be accepted",
  requirements_created_by: "vakho",
  generation_status: "GENERATED",
  generation_message: null,
  generated_tests: [{ filename: "test_quantity.py", relative_path: ".ai-platform/tests/generated/test_quantity.py", source: "GENERATED", content: "def test_quantity(): pass", created_by: "claude", created_at: "2026-01-01T00:00:00Z", requirement_mapping: { R1: ["Quantity 10"] } }],
  uploaded_tests: [{ filename: "custom.py", relative_path: ".ai-platform/tests/uploaded/custom.py", source: "UPLOADED", content: "assert True", created_by: "vakho", created_at: "2026-01-01T00:00:00Z", requirement_mapping: {} }],
  repository_tests: ["tests/test_service.py"],
  latest_verification: { status: "PASS", sequence_id: 4, timestamp: "2026-01-01T00:00:00Z", exit_code: 0, duration_seconds: 1, timed_out: false, stdout: "", stderr: "", error: null, verification_mode: "BASELINE_AWARE", task_specific_targets: [], task_specific_passed: true, task_specific_passed_tests: 1, task_acceptance_status: "PASS", task_acceptance_targets: [], broad_regression_passed: true, baseline_warning_count: 0, new_regression_count: 0, pre_existing_failures: [], fixed_failures: [], new_failures: [] },
} as const;

describe("Tests tab", () => {
  it("renders human, generated, uploaded, repository, and canonical verification sources", async () => {
    getTaskTests.mockResolvedValue(response);
    render(<TestsTab taskId="TASK-1" events={[]} />);
    await waitFor(() => expect(screen.getByText(/Quantity 10 should be accepted/)).toBeTruthy());
    expect(screen.getByText(".ai-platform/tests/generated/test_quantity.py")).toBeTruthy();
    expect(screen.getByText(".ai-platform/tests/uploaded/custom.py")).toBeTruthy();
    expect(screen.getByText("tests/test_service.py")).toBeTruthy();
    expect(screen.getByText("Overall verification")).toBeTruthy();
  });

  it("shows NOT_PROVIDED without inventing verification", async () => {
    getTaskTests.mockResolvedValue({ ...response, human_requirements: null, generated_tests: [], uploaded_tests: [], latest_verification: null });
    render(<TestsTab taskId="TASK-1" events={[]} />);
    expect(await screen.findByText("No tests supplied")).toBeTruthy();
    expect(screen.getByText("No canonical verification result is available yet.")).toBeTruthy();
  });
});
