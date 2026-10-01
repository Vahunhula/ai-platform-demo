// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TaskSidebar } from "./TaskSidebar";
afterEach(cleanup);
it("selects a task from the compact task list", () => {
  const onSelect = vi.fn();
  render(<TaskSidebar tasks={[{ id:"TASK-1",title:"Addition",difficulty:"MEDIUM",status:"READY",workflow_phase:"BRAINSTORM",model_tier:null,writer:null,disposition:null,updated_at:"2026-01-01T00:00:00Z" }]} selectedId={null} onSelect={onSelect} onNewTask={vi.fn()} canCreate user={{ id:"u",username:"v",display_name:"Vakho",role:"developer",can_modify_tasks:true }} onSignOut={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: /TASK-1/ }));
  expect(onSelect).toHaveBeenCalledWith("TASK-1");
});
