export type TaskTab = "Chat" | "Changes" | "Tests" | "Activity" | "Summary" | "Workspace";
export const TASK_TABS: TaskTab[] = ["Chat", "Changes", "Tests", "Activity", "Summary", "Workspace"];

export function TaskTabs({ active, onChange }: { active: TaskTab; onChange: (tab: TaskTab) => void }) {
  return <div className="tabs" role="tablist" aria-label="Task activity">{TASK_TABS.map((tab) => (
    <button key={tab} role="tab" aria-selected={active === tab} className={active === tab ? "active" : ""} onClick={() => onChange(tab)}>{tab}</button>
  ))}</div>;
}
