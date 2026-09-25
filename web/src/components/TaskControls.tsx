import { useEffect, useState } from "react";

import { ApiError, api } from "../api/client";
import { newClientId } from "../api/ids";
import type { ControlAction, ControlRequest, ControlResponse, TaskDetail } from "../types/api";
import { ConfirmDialog } from "./ConfirmDialog";

const ORDER: ControlAction[] = ["start", "resume", "pause", "approve", "defer", "reject", "reset"];
const LABEL: Record<ControlAction, string> = {
  start: "Start",
  resume: "Resume",
  pause: "Pause",
  approve: "Approve",
  defer: "Defer…",
  reject: "Reject…",
  reset: "Reset…",
};
// Start and pause act immediately; the others open a dialog first.
const NEEDS_DIALOG = new Set<ControlAction>(["resume", "approve", "defer", "reject", "reset"]);

interface Feedback {
  tone: "info" | "error";
  text: string;
  /** Present when the failure is retryable with the same idempotency key. */
  retry?: ControlRequest;
}

function successText(response: ControlResponse): string {
  const suffix = response.duplicate ? " (already accepted earlier)" : "";
  switch (response.action) {
    case "start":
      return `Start accepted — the agent turn is running.${suffix}`;
    case "resume":
      return `Resume accepted — the agent continues in the same workspace.${suffix}`;
    case "reject":
      return `Rejection recorded — a correction turn is running.${suffix}`;
    case "pause":
      return response.deferred
        ? "Pause requested — the agent stops at its next safe point."
        : "Task paused.";
    case "approve":
      return "Task approved and completed. Nothing was committed, pushed or merged.";
    case "defer":
      return "Task deferred and closed. Nothing was committed, pushed or merged.";
    case "reset":
      return "Task reset to READY. Its history is kept.";
  }
}

interface Props {
  detail: TaskDetail;
  onChanged: () => void;
  onRemoved: () => void;
}

export function TaskControls({ detail, onChanged, onRemoved }: Props) {
  const [dialog, setDialog] = useState<ControlAction | "remove" | null>(null);
  const [dialogText, setDialogText] = useState("");
  const [dialogKey, setDialogKey] = useState("");
  const [busy, setBusy] = useState<ControlAction | "remove" | null>(null);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);

  useEffect(() => {
    setDialog(null);
    setFeedback(null);
  }, [detail.id]);

  const allowed = ORDER.filter((action) => detail.actions[action].allowed);
  const unavailable = ORDER.filter((action) => !detail.actions[action].allowed);

  async function perform(body: ControlRequest, fromDialog: boolean) {
    setBusy(body.action);
    setFeedback(null);
    setDialogError(null);
    try {
      const response = await api.control(detail.id, body);
      setFeedback({ tone: "info", text: successText(response) });
      setDialog(null);
      onChanged();
    } catch (reason) {
      const error = reason instanceof ApiError ? reason : new ApiError(String(reason), 0);
      if (fromDialog) setDialogError(error.message);
      else
        setFeedback({
          tone: "error",
          text: error.message,
          retry: error.retryable ? body : undefined,
        });
      if (!error.retryable) onChanged(); // state moved on; show the backend's view
    } finally {
      setBusy(null);
    }
  }

  async function removeTask() {
    setBusy("remove");
    setDialogError(null);
    try {
      await api.removeTask(detail.id);
      setDialog(null);
      onRemoved();
    } catch (reason) {
      const error = reason instanceof ApiError ? reason : new ApiError(String(reason), 0);
      setDialogError(error.message);
      if (!error.retryable) onChanged();
    } finally {
      setBusy(null);
    }
  }

  function click(action: ControlAction) {
    if (NEEDS_DIALOG.has(action)) {
      setDialog(action);
      setDialogText("");
      setDialogError(null);
      setDialogKey(newClientId()); // one idempotency key per dialog = per user intent
      return;
    }
    if (action === "start") void perform({ action, client_action_id: newClientId() }, false);
    if (action === "pause") void perform({ action }, false);
  }

  function confirm() {
    if (dialog === "resume")
      void perform({ action: "resume", client_action_id: dialogKey, message: dialogText || null }, true);
    if (dialog === "reject")
      void perform({ action: "reject", client_action_id: dialogKey, message: dialogText }, true);
    if (dialog === "approve") void perform({ action: "approve" }, true);
    if (dialog === "defer") void perform({ action: "defer" }, true);
    if (dialog === "reset") void perform({ action: "reset", confirm: true }, true);
    if (dialog === "remove") void removeTask();
  }

  return (
    <div className="controls">
      <div className="control-buttons">
        {allowed.map((action) => (
          <button
            key={action}
            className={`control ${action === "reset" ? "danger-outline" : action === "pause" ? "secondary" : "primary"}`}
            onClick={() => click(action)}
            disabled={busy !== null}
          >
            {busy === action ? "Working…" : LABEL[action]}
          </button>
        ))}
        {detail.can_remove && (
          <button
            className="control danger"
            onClick={() => {
              setDialog("remove");
              setDialogError(null);
            }}
            disabled={busy !== null}
          >
            {busy === "remove" ? "Removing…" : "Remove task…"}
          </button>
        )}
        {allowed.length === 0 && (
          <span className="muted">
            {detail.pause_requested
              ? "Pause requested — waiting for the agent to reach a safe point."
              : "No lifecycle actions are available right now."}
          </span>
        )}
        {unavailable.length > 0 && (
          <details className="why">
            <summary>Why not…</summary>
            <ul>
              {unavailable.map((action) => (
                <li key={action}>
                  <strong>{LABEL[action].replace("…", "")}:</strong> {detail.actions[action].reason}
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
      {feedback && (
        <div className={`control-feedback ${feedback.tone}`} role="status">
          {feedback.text}
          {feedback.retry && (
            <button className="link-button" onClick={() => void perform(feedback.retry!, false)}>
              Retry
            </button>
          )}
        </div>
      )}

      {dialog === "resume" && (
        <ConfirmDialog
          title={`Resume ${detail.id}`}
          confirmLabel="Resume"
          busy={busy !== null}
          error={dialogError}
          onConfirm={confirm}
          onCancel={() => setDialog(null)}
        >
          <p>
            Claude continues this TaskSession from the <strong>current workspace</strong>, including
            any manual edits made while paused.
          </p>
          <label className="dialog-label" htmlFor="resume-message">
            Optional instruction
          </label>
          <textarea
            id="resume-message"
            rows={3}
            maxLength={8000}
            value={dialogText}
            onChange={(event) => setDialogText(event.target.value)}
          />
        </ConfirmDialog>
      )}
      {dialog === "reject" && (
        <ConfirmDialog
          title={`Reject ${detail.id} with feedback`}
          confirmLabel="Reject and correct"
          busy={busy !== null}
          confirmDisabled={!dialogText.trim()}
          error={dialogError}
          onConfirm={confirm}
          onCancel={() => setDialog(null)}
        >
          <p>
            Rejecting does <strong>not</strong> fail the task. Your feedback is recorded and Claude
            immediately runs a correction turn in the same workspace, followed by verification.
          </p>
          <label className="dialog-label" htmlFor="reject-message">
            Feedback (required)
          </label>
          <textarea
            id="reject-message"
            rows={4}
            maxLength={8000}
            value={dialogText}
            onChange={(event) => setDialogText(event.target.value)}
          />
        </ConfirmDialog>
      )}
      {dialog === "approve" && (
        <ConfirmDialog
          title={`Approve ${detail.id}`}
          confirmLabel="Approve"
          busy={busy !== null}
          error={dialogError}
          onConfirm={confirm}
          onCancel={() => setDialog(null)}
        >
          <p>The platform task becomes COMPLETED. Only a reset can reopen it.</p>
          <p>
            Nothing is committed, pushed, merged or deployed; the workspace is kept for
            inspection.
          </p>
        </ConfirmDialog>
      )}
      {dialog === "defer" && (
        <ConfirmDialog
          title={`Defer ${detail.id}`}
          confirmLabel="Defer task"
          busy={busy !== null}
          error={dialogError}
          onConfirm={confirm}
          onCancel={() => setDialog(null)}
        >
          <p>
            This records the terminal <strong>DEFERRED</strong> disposition and stops work on the
            task. The workspace and history remain until explicitly removed.
          </p>
        </ConfirmDialog>
      )}
      {dialog === "reset" && (
        <ConfirmDialog
          title={`Reset ${detail.id}?`}
          confirmLabel="Delete workspace and reset"
          tone="danger"
          busy={busy !== null}
          error={dialogError}
          onConfirm={confirm}
          onCancel={() => setDialog(null)}
        >
          <ul>
            <li>
              <strong>Deletes</strong> the task workspace
              {detail.workspace_id ? ` (${detail.workspace_id})` : ""}, including any uncommitted
              changes in it.
            </li>
            <li>Returns the task to READY and clears its model tier, attempts and verification.</li>
            <li>
              <strong>Keeps</strong> the full history: trace, tests and conversation.
            </li>
          </ul>
        </ConfirmDialog>
      )}
      {dialog === "remove" && (
        <ConfirmDialog
          title="Remove task permanently?"
          confirmLabel="Remove permanently"
          tone="danger"
          busy={busy !== null}
          error={dialogError}
          onConfirm={confirm}
          onCancel={() => setDialog(null)}
        >
          <p>This will permanently delete:</p>
          <ul>
            <li>task conversation</li>
            <li>workflow history and artifacts</li>
            <li>workspace</li>
            <li>AI session metadata</li>
          </ul>
          <p><strong>This cannot be undone.</strong></p>
        </ConfirmDialog>
      )}
    </div>
  );
}
