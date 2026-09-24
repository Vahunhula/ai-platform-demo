import type { TaskDetail } from "../types/api";
import { formatDateTime } from "./format";

export function TaskOverview({ detail }: { detail: TaskDetail }) {
  const verification = detail.verification_result;
  return (
    <section className="overview">
      <div className="facts">
        <Fact label="Phase" value={detail.workflow_phase.replaceAll("_", " ")} />
        <Fact label="Difficulty" value={detail.difficulty} />
        <Fact
          label="Model"
          value={[detail.model_tier, detail.model_name].filter(Boolean).join(" / ") || "Not selected"}
        />
        <Fact label="Attempt" value={String(detail.current_attempt)} />
        <Fact
          label="Verification"
          value={
            verification
              ? `${detail.verification_status} (#${verification.sequence_id}, exit ${verification.exit_code ?? "?"})`
              : detail.verification_status
          }
        />
        <Fact label="Active writer" value={detail.writer ?? "None"} />
        <Fact label="Workspace" value={detail.workspace_id ?? "Not created"} />
      </div>
      <div className="overview-copy">
        <div>
          <h3>Description</h3>
          <p>{detail.description}</p>
          <p className="timestamps">
            Created {formatDateTime(detail.created_at)}
            <br />
            Updated {formatDateTime(detail.updated_at)}
          </p>
        </div>
        <div>
          <h3>Acceptance criteria</h3>
          <ul>
            {detail.acceptance_criteria.map((criterion) => (
              <li key={criterion}>{criterion}</li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="fact">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
