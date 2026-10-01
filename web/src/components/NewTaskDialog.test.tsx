// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NewTaskDialog } from "./NewTaskDialog";

const mocks = vi.hoisted(() => ({
  listRepositories: vi.fn(),
  listAssignableUsers: vi.fn(),
  createTask: vi.fn(),
}));

vi.mock("../api/client", () => ({ api: mocks }));

beforeEach(() => {
  mocks.listRepositories.mockResolvedValue([{ id: "repo", slug: "repo", display_name: "Repo", default_branch: "main", enabled: true }]);
  mocks.listAssignableUsers.mockResolvedValue([{ id: "user", username: "vakho", display_name: "Vakho" }]);
  mocks.createTask.mockResolvedValue({ id: "TASK-1" });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

async function fillAndSubmit(stories = "") {
  await screen.findByRole("option", { name: "Repo" });
  fireEvent.change(screen.getByLabelText(/Title/), { target: { value: "Addition" } });
  fireEvent.change(screen.getByLabelText(/Description/), { target: { value: "Add values" } });
  if (stories) fireEvent.change(screen.getByLabelText("Acceptance / user stories"), { target: { value: stories } });
  fireEvent.click(screen.getByRole("button", { name: "Create Task" }));
  await waitFor(() => expect(mocks.createTask).toHaveBeenCalledOnce());
}

function textFile(name: string, content: string): File {
  const file = new File([content], name, { type: "text/plain" });
  Object.defineProperty(file, "text", { value: () => Promise.resolve(content) });
  return file;
}

describe("New Task", () => {
  it("creates without optional tests", async () => {
    render(<NewTaskDialog onClose={vi.fn()} onCreated={vi.fn()} />);
    await fillAndSubmit();
    expect(mocks.createTask.mock.calls[0][0]).toMatchObject({ acceptance_test_stories: null, uploaded_test_files: [] });
  });

  it("preserves human stories exactly", async () => {
    render(<NewTaskDialog onClose={vi.fn()} onCreated={vi.fn()} />);
    await fillAndSubmit("  - Quantity 10 works\n");
    expect(mocks.createTask.mock.calls[0][0].acceptance_test_stories).toBe("  - Quantity 10 works\n");
  });

  it.each([
    ["uploaded file", "", [textFile("test_case.py", "assert True")]],
    ["stories and uploaded file", "- Works", [textFile("cases.yaml", "ok: true")]],
  ])("creates with %s", async (_label, stories, files) => {
    render(<NewTaskDialog onClose={vi.fn()} onCreated={vi.fn()} />);
    await screen.findByRole("option", { name: "Repo" });
    fireEvent.change(screen.getByLabelText("Upload test files"), { target: { files } });
    await screen.findByText(files[0].name);
    await fillAndSubmit(stories);
    expect(mocks.createTask.mock.calls[0][0].uploaded_test_files[0]).toEqual({ filename: files[0].name, content: expect.any(String) });
  });

  it("rejects unsupported files before creation", async () => {
    render(<NewTaskDialog onClose={vi.fn()} onCreated={vi.fn()} />);
    await screen.findByRole("option", { name: "Repo" });
    fireEvent.change(screen.getByLabelText("Upload test files"), { target: { files: [textFile("notes.txt", "no")] } });
    expect(await screen.findByText(/Unsupported test file: notes.txt/)).toBeTruthy();
    expect(mocks.createTask).not.toHaveBeenCalled();
  });
});
