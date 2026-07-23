"use client";

import { useEffect, useRef, useState } from "react";
import { JobControls } from "@/components/JobControls";
import { getJob, jobZipUrl, type JobSummary } from "@/lib/api";
import { JOB_EVENT_TYPES, type JobEvent } from "@/lib/events";

function describe(ev: JobEvent): string {
  switch (ev.type) {
    case "queued":
      return "Queued";
    case "started":
      return "Started";
    case "prepare":
      return `Fetching ${ev.filename ?? "…"}`;
    case "file":
      return `${ev.event === "skipped" ? "↷ skipped" : "✓ downloaded"} ${ev.filename ?? ""}${
        ev.bytes ? ` (${ev.bytes} B)` : ""
      }`;
    case "progress":
      return `progress: ${ev.downloaded ?? 0} downloaded, ${ev.skipped ?? 0} skipped, ${
        ev.failed ?? 0
      } failed`;
    case "heartbeat":
      return `· still working (${Math.round(ev.elapsed ?? 0)}s)`;
    case "stalled":
      return ev.phase === "warmup"
        ? `⏳ no files yet after ${Math.round(ev.threshold ?? 0)}s (attempt ${ev.attempt ?? 1})`
        : `⏳ stalled — no progress for ${Math.round(ev.since_last_file ?? 0)}s (attempt ${
            ev.attempt ?? 1
          })`;
    case "retrying":
      return `↻ retrying (attempt ${ev.attempt ?? 1})`;
    case "paused":
      return "⏸ Paused — worker suspended, queue slot released";
    case "resumed":
      return `▶ Resumed after ${Math.round(ev.paused_for ?? 0)}s`;
    case "error":
      return `${ev.fatal ? "⛔ " : "⚠ "}${ev.message ?? ev.kind ?? "error"}`;
    case "completed":
      return `Completed — ${ev.downloaded ?? 0} file(s)`;
    case "cancelled":
      return `⏹ Stopped — ${ev.downloaded ?? 0} file(s) kept`;
    case "failed":
      return ev.reason === "rate-limited"
        ? "⏸ Stopped — the platform rate-limited this account"
        : `Failed — ${ev.reason ?? "unknown"}`;
    default:
      return ev.type;
  }
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    connecting: "badge-ghost",
    queued: "badge-warning",
    running: "badge-info",
    started: "badge-info",
    paused: "badge-warning",
    completed: "badge-success",
    failed: "badge-error",
    cancelled: "badge-ghost",
  };
  return <span className={`badge ${map[status] ?? "badge-ghost"}`}>{status}</span>;
}

const TERMINAL_STATUSES = ["completed", "failed", "cancelled"];

export function JobProgress({ jobId }: { jobId: string }) {
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [summary, setSummary] = useState<JobSummary | null>(null);
  const [status, setStatus] = useState("connecting");
  const [counts, setCounts] = useState({ downloaded: 0, skipped: 0, failed: 0 });
  const [stall, setStall] = useState<string | null>(null);
  const [alive, setAlive] = useState<number | null>(null);
  // The terminal event as it arrives on the stream — without this the failure alert below only
  // renders after a reload, because `summary` is fetched once at mount.
  const [terminalEvent, setTerminalEvent] = useState<JobEvent | null>(null);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    let cancelled = false;
    getJob(jobId)
      .then((s) => {
        if (!cancelled) {
          setSummary(s);
          setStatus(s.status);
        }
      })
      .catch(() => {});

    const es = new EventSource(`/api/jobs/${jobId}/events`);
    esRef.current = es;

    const apply = (ev: JobEvent) => {
      setEvents((prev) => [...prev, ev]);
      if (ev.type === "progress") {
        setCounts({
          downloaded: ev.downloaded ?? 0,
          skipped: ev.skipped ?? 0,
          failed: ev.failed ?? 0,
        });
      }
      if (ev.type === "heartbeat") {
        setAlive(ev.elapsed ?? null);
      }
      if (ev.type === "stalled") {
        setStall(
          ev.phase === "warmup"
            ? "Still no files — the profile may be large, restricted, or throttled. Retrying…"
            : "Stalled — no progress; retrying…",
        );
      } else if (ev.type === "retrying") {
        setStall(`Retrying (attempt ${ev.attempt ?? 1})…`);
      } else if (
        ev.type === "file" ||
        ev.type === "progress" ||
        TERMINAL_STATUSES.includes(ev.type)
      ) {
        setStall(null);
      }
      // Pause/resume also arrive over SSE, so the badge stays right even when the action was
      // taken from the Queue page in another tab.
      if (ev.type === "paused") setStatus("paused");
      if (ev.type === "resumed") setStatus("running");
      if (TERMINAL_STATUSES.includes(ev.type)) {
        setStatus(ev.type);
        setTerminalEvent(ev);
      }
    };
    const onMsg = (e: MessageEvent) => {
      try {
        apply(JSON.parse(e.data) as JobEvent);
      } catch {
        /* ignore malformed */
      }
    };
    JOB_EVENT_TYPES.forEach((t) => es.addEventListener(t, onMsg));
    es.addEventListener("end", () => es.close());
    es.onerror = () => {
      /* EventSource auto-reconnects; backend replays history on connect */
    };

    return () => {
      cancelled = true;
      es.close();
    };
  }, [jobId]);

  const terminal = TERMINAL_STATUSES.includes(status);
  // Prefer the live terminal event; fall back to the summary fetched at mount (page reload case).
  const finalReason = (terminalEvent ??
    (summary?.final_summary as
      | { reason?: string; message?: string; resume_url?: string }
      | undefined)) as
    | { reason?: string; message?: string; resume_url?: string }
    | undefined;

  return (
    <div className="flex flex-col gap-4">
      <div className="card bg-base-100 shadow-xl border border-base-300">
        <div className="card-body gap-3">
          <div className="flex items-center justify-between gap-2">
            <h2 className="card-title truncate">Job {jobId.slice(0, 12)}…</h2>
            <div className="flex items-center gap-2">
              <StatusBadge status={status} />
              <JobControls jobId={jobId} status={status} onChanged={setStatus} />
            </div>
          </div>
          {summary && (
            <p className="text-sm text-base-content/70 break-all">{summary.url}</p>
          )}
          <div className="stats stats-vertical sm:stats-horizontal shadow bg-base-200">
            <div className="stat">
              <div className="stat-title">Downloaded</div>
              <div className="stat-value text-success">{counts.downloaded}</div>
            </div>
            <div className="stat">
              <div className="stat-title">Skipped</div>
              <div className="stat-value">{counts.skipped}</div>
            </div>
            <div className="stat">
              <div className="stat-title">Failed</div>
              <div className="stat-value text-error">{counts.failed}</div>
            </div>
          </div>
          {status === "paused" && (
            <div className="alert alert-warning flex-col items-start gap-1 py-2 text-sm">
              <span className="font-semibold">⏸ Paused — the worker is suspended.</span>
              <span>
                It keeps its place in the profile, so resuming continues from here instead of
                starting the walk over. Its queue slot has been freed for another profile. A very
                long pause can drop the connection to the platform; gallery-dl retries when it
                resumes.
              </span>
            </div>
          )}
          {!terminal && status !== "paused" && stall && (
            <div className="alert alert-warning py-2 text-sm">{stall}</div>
          )}
          {status === "queued" && summary?.started_at != null && (
            <div className="alert alert-info py-2 text-sm">
              Resuming — waiting for a free download slot.
            </div>
          )}
          {!terminal && status !== "paused" && !stall && alive !== null && counts.downloaded + counts.skipped === 0 && (
            <div className="alert alert-info py-2 text-sm">
              Looking through the profile… ({Math.round(alive)}s). Large or rate-limited profiles
              can take several minutes before the first file appears.
            </div>
          )}
          {terminal && status !== "failed" && counts.downloaded > 0 && (
            <a className="btn btn-primary btn-sm self-start" href={jobZipUrl(jobId)}>
              ⬇ Download all (.zip)
            </a>
          )}
          {status === "cancelled" && (
            <div className="alert flex-col items-start gap-1 py-2 text-sm">
              <span className="font-semibold">⏹ Stopped.</span>
              <span>
                {counts.downloaded} file(s) downloaded before you stopped were kept, and this
                profile&apos;s gallery has been updated. Re-running it skips them and continues.
              </span>
            </div>
          )}
          {terminal && status === "failed" && finalReason && (
            <div
              className={`alert flex-col items-start gap-1 py-2 text-sm ${
                // A rate limit is the platform pausing us, not a failure of the app — don't
                // shout at the user in red for something they can only wait out.
                finalReason.reason === "rate-limited" ? "alert-warning" : "alert-error"
              }`}
            >
              {finalReason.reason === "missing-cookies" ? (
                <>
                  No cookies configured for this platform.{" "}
                  <a className="link" href="/settings">
                    Add them in Settings
                  </a>
                  .
                </>
              ) : finalReason.reason === "rate-limited" ? (
                <>
                  <span className="font-semibold">⏸ Paused by the platform, not an error.</span>
                  <span>{String(finalReason.message ?? "")}</span>
                  {counts.downloaded + counts.skipped > 0 && (
                    <span className="opacity-80">
                      {counts.downloaded} file(s) downloaded before the block were saved — a later
                      re-run skips them and continues.
                    </span>
                  )}
                  {finalReason.resume_url && (
                    <span className="break-all font-mono text-xs opacity-80">
                      Resume point: {finalReason.resume_url}
                    </span>
                  )}
                </>
              ) : finalReason.reason === "downloads-dir-unwritable" ? (
                <>
                  <span className="font-semibold">Downloads folder is not writable.</span>
                  <span className="whitespace-pre-wrap break-all font-mono text-xs">
                    {String(finalReason.message ?? "")}
                  </span>
                </>
              ) : finalReason.reason === "no-progress" ? (
                <>
                  <span className="font-semibold">
                    No files were found before the timeout.
                  </span>
                  <span className="whitespace-pre-wrap break-all font-mono text-xs">
                    {String(finalReason.message ?? "")}
                  </span>
                </>
              ) : (
                <span className="whitespace-pre-wrap break-all">
                  {String(finalReason.message ?? finalReason.reason ?? "Download failed")}
                </span>
              )}
            </div>
          )}
        </div>
      </div>

      <div className="card bg-base-100 shadow border border-base-300">
        <div className="card-body">
          <h3 className="font-semibold">Activity</h3>
          <ul className="flex flex-col gap-1 max-h-96 overflow-y-auto font-mono text-xs">
            {events.map((ev, i) => (
              <li key={i} className="text-base-content/80">
                {describe(ev)}
              </li>
            ))}
            {!terminal && <li className="text-base-content/50 animate-pulse">working…</li>}
          </ul>
        </div>
      </div>
    </div>
  );
}
