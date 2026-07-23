"use client";

import { useState } from "react";
import { cancelJob, pauseJob, resumeJob } from "@/lib/api";

/**
 * Pause / Resume / Stop for one job. Shared by the queue rows and the job detail page.
 *
 * Which buttons apply is derived from `status` alone, matching the backend state machine:
 * queued and running can be paused or stopped, paused can be resumed or stopped, and a terminal
 * job accepts nothing (the backend answers 409, which is surfaced as the inline error).
 */
export function JobControls({
  jobId,
  status,
  size = "sm",
  onChanged,
}: {
  jobId: string;
  status: string;
  size?: "xs" | "sm";
  onChanged?: (status: string) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const terminal = status === "completed" || status === "failed" || status === "cancelled";
  const paused = status === "paused";
  if (terminal) return null;

  async function run(action: "pause" | "resume" | "cancel") {
    setBusy(action);
    setError(null);
    try {
      const fn = action === "pause" ? pauseJob : action === "resume" ? resumeJob : cancelJob;
      const updated = await fn(jobId);
      onChanged?.(updated.status);
    } catch (err) {
      setError(err instanceof Error ? err.message : `could not ${action} this download`);
    } finally {
      setBusy(null);
    }
  }

  const btn = `btn btn-${size}`;
  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex flex-wrap gap-1">
        {paused ? (
          <button className={`${btn} btn-primary`} disabled={!!busy} onClick={() => run("resume")}>
            ▶ Resume
          </button>
        ) : (
          <button className={`${btn} btn-ghost`} disabled={!!busy} onClick={() => run("pause")}>
            ⏸ Pause
          </button>
        )}
        <button className={`${btn} btn-ghost text-error`} disabled={!!busy} onClick={() => run("cancel")}>
          ⏹ Stop
        </button>
      </div>
      {error && <span className="text-xs text-error">{error}</span>}
    </div>
  );
}
