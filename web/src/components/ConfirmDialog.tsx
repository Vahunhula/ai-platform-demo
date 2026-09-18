import { type ReactNode, useEffect, useRef } from "react";

interface Props {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  tone?: "default" | "danger";
  busy?: boolean;
  confirmDisabled?: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Small in-app confirmation dialog (Escape or backdrop click cancels). */
export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  tone = "default",
  busy = false,
  confirmDisabled = false,
  error,
  onConfirm,
  onCancel,
}: Props) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKey);
    panelRef.current?.querySelector<HTMLElement>("textarea, button.dialog-confirm")?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onCancel]);

  return (
    <div className="dialog-backdrop" onMouseDown={() => !busy && onCancel()}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="dialog-title"
        ref={panelRef}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <h3 id="dialog-title">{title}</h3>
        <div className="dialog-body">{children}</div>
        {error && <div className="composer-error">{error}</div>}
        <div className="dialog-actions">
          <button className="control secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            className={`control dialog-confirm ${tone === "danger" ? "danger" : "primary"}`}
            onClick={onConfirm}
            disabled={busy || confirmDisabled}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
