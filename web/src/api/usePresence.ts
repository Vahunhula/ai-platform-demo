import { useEffect, useState } from "react";

import type { PresenceUser } from "../types/api";
import { api } from "./client";

/**
 * Heartbeat (POST) the current user's presence on a task while it is open, and
 * return who is viewing it. Presence is ephemeral: it expires server-side after a
 * TTL and never enters the task's history.
 */
export function usePresence(taskId: string | null, intervalSeconds: number): PresenceUser[] {
  const [viewers, setViewers] = useState<PresenceUser[]>([]);

  useEffect(() => {
    setViewers([]);
    if (!taskId) return;
    let cancelled = false;
    const beat = () =>
      api
        .heartbeat(taskId)
        .then((response) => {
          if (!cancelled && response.task_id === taskId) setViewers(response.viewers);
        })
        .catch(() => undefined);
    void beat();
    const timer = window.setInterval(beat, intervalSeconds * 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [taskId, intervalSeconds]);

  return viewers;
}
