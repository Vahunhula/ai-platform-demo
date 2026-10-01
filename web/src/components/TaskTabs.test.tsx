// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TaskTabs } from "./TaskTabs";
afterEach(cleanup);
it("switches primary task tabs", () => {
  const onChange = vi.fn();
  render(<TaskTabs active="Chat" onChange={onChange} />);
  expect(screen.getByRole("tab", { name: "Chat" }).getAttribute("aria-selected")).toBe("true");
  fireEvent.click(screen.getByRole("tab", { name: "Tests" }));
  expect(onChange).toHaveBeenCalledWith("Tests");
});
