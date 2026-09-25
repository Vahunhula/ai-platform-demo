import type { PlatformEvent, TaskDetail } from "../types/api";

const PHASES: TaskDetail["workflow_phase"][] = [
  "BRAINSTORM",
  "PLAN",
  "IMPLEMENTATION",
  "REVIEW",
  "HUMAN_REVIEW",
];

const PHASE_LABEL: Record<string, string> = {
  BRAINSTORM: "Brainstorm",
  PLAN: "Plan",
  IMPLEMENTATION: "Implementation",
  REVIEW: "Review",
  HUMAN_REVIEW: "Human Review",
};

/** Phases the durable history actually ran, so a manual backtrack still shows as visited. */
function visitedPhases(events: PlatformEvent[]): Set<string> {
  const visited = new Set<string>();
  for (const event of events) {
    if (
      event.event_type === "WORKFLOW_PHASE_STARTED" ||
      event.event_type === "WORKFLOW_PHASE_OUTPUT_CREATED"
    ) {
      const phase = event.metadata.phase;
      if (typeof phase === "string") visited.add(phase);
    }
  }
  return visited;
}

export function WorkflowProgress({ detail, events }: { detail: TaskDetail; events: PlatformEvent[] }) {
  const currentIndex = PHASES.indexOf(detail.workflow_phase);
  const visited = visitedPhases(events);
  const waiting = detail.status === "WAITING_FOR_HUMAN";
  return (
    <div className="workflow-progress" role="list" aria-label="Workflow progress">
      {PHASES.map((phase, index) => {
        const isCurrent = phase === detail.workflow_phase;
        const state = isCurrent
          ? waiting
            ? "waiting"
            : "current"
          : index < currentIndex || visited.has(phase)
            ? "done"
            : "upcoming";
        return (
          <div className="workflow-step" role="listitem" key={phase}>
            <span className={`workflow-dot workflow-${state}`} aria-hidden="true" />
            <span className={`workflow-label ${isCurrent ? "current" : ""}`}>
              {PHASE_LABEL[phase] ?? phase}
            </span>
            {isCurrent && detail.latest_readiness && detail.latest_readiness.phase === phase && (
              <span className="workflow-readiness">{detail.latest_readiness.score.toFixed(0)}%</span>
            )}
            {index < PHASES.length - 1 && <span className="workflow-connector" aria-hidden="true" />}
          </div>
        );
      })}
    </div>
  );
}
