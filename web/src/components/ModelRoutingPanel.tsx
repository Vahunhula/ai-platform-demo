import { useEffect, useState } from "react";

import { api, ApiError } from "../api/client";
import type { CurrentUser, LogicalModel, ModelCatalogEntry, TaskModelRouting } from "../types/api";

export function ModelRoutingPanel({ taskId, user }: { taskId: string; user: CurrentUser }) {
  const [catalog, setCatalog] = useState<ModelCatalogEntry[]>([]);
  const [routing, setRouting] = useState<TaskModelRouting | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const load = async (signal?: AbortSignal) => {
    try {
      const [models, value] = await Promise.all([
        api.listModels(signal),
        api.getModelRouting(taskId, signal),
      ]);
      setCatalog(models);
      setRouting(value);
      setError("");
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      setError(reason instanceof ApiError ? reason.message : "Could not load model routing.");
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [taskId]);

  const update = async (operation: () => Promise<unknown>) => {
    setSaving(true);
    try {
      await operation();
      await load();
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "Could not save model routing.");
    } finally {
      setSaving(false);
    }
  };

  if (!routing) return <section className="model-routing"><h3>Model routing</h3><p>{error || "Loading…"}</p></section>;
  const disabled = saving || !user.can_modify_tasks;
  const options = catalog.map((entry) => (
    <option key={entry.logical_id} value={entry.logical_id} disabled={!entry.enabled}>
      {entry.display_name}{entry.enabled ? "" : " (unavailable)"}
    </option>
  ));
  return (
    <section className="model-routing">
      <h3>Model routing</h3>
      <label>Default model
        <select disabled={disabled} value={routing.default_model_selection}
          onChange={(event) => void update(() => api.setDefaultModel(taskId, event.target.value as LogicalModel))}>
          {options}
        </select>
      </label>
      <div className="phase-models">
        {routing.phases.map((phase) => (
          <label key={phase.phase}>{phase.phase.replaceAll("_", " ")}
            <select disabled={disabled} value={phase.selection}
              onChange={(event) => void update(() => api.setPhaseModel(taskId, phase.phase, event.target.value as LogicalModel))}>
              {options}
            </select>
            <small>{phase.resolved ? `Next: ${phase.resolved.effective_selection.replace("CLAUDE_", "Claude ")}` : phase.error}</small>
          </label>
        ))}
      </div>
      <p className="muted">Auto inherits the task default, then uses platform policy. Changes apply to future execution; a running turn keeps its snapshotted model.</p>
      {!user.can_modify_tasks && <p className="muted">Read-only access</p>}
      {error && <p className="error">{error}</p>}
    </section>
  );
}
