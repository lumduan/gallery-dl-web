"use client";

import { useEffect, useRef, useState } from "react";
import { getJob, jobZipUrl, type JobSummary } from "@/lib/api";
import type { JobEvent } from "@/lib/events";

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
    case "error":
      return `${ev.fatal ? "⛔ " : "⚠ "}${ev.message ?? ev.kind ?? "error"}`;
    case "completed":
      return `Completed — ${ev.downloaded ?? 0} file(s)`;
    case "failed":
      return `Failed — ${ev.reason ?? "unknown"}`;
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
    completed: "badge-success",
    failed: "badge-error",
  };
  return <span className={`badge ${map[status] ?? "badge-ghost"}`}>{status}</span>;
}

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
        ev.type === "completed" ||
        ev.type === "failed"
      ) {
        setStall(null);
      }
      if (ev.type === "completed" || ev.type === "failed") {
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
    [
      "queued",
      "started",
      "prepare",
      "file",
      "progress",
      "heartbeat",
      "stalled",
      "retrying",
      "error",
      "completed",
      "failed",
    ].forEach((t) => es.addEventListener(t, onMsg));
    es.addEventListener("end", () => es.close());
    es.onerror = () => {
      /* EventSource auto-reconnects; backend replays history on connect */
    };

    return () => {
      cancelled = true;
      es.close();
    };
  }, [jobId]);

  const terminal = status === "completed" || status === "failed";
  // Prefer the live terminal event; fall back to the summary fetched at mount (page reload case).
  const finalReason = (terminalEvent ??
    (summary?.final_summary as { reason?: string; message?: string } | undefined)) as
    | { reason?: string; message?: string }
    | undefined;

  return (
    <div className="flex flex-col gap-4">
      <div className="card bg-base-100 shadow-xl border border-base-300">
        <div className="card-body gap-3">
          <div className="flex items-center justify-between gap-2">
            <h2 className="card-title truncate">Job {jobId.slice(0, 12)}…</h2>
            <StatusBadge status={status} />
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
          {!terminal && stall && (
            <div className="alert alert-warning py-2 text-sm">{stall}</div>
          )}
          {!terminal && !stall && alive !== null && counts.downloaded + counts.skipped === 0 && (
            <div className="alert alert-info py-2 text-sm">
              Looking through the profile… ({Math.round(alive)}s). Large or rate-limited profiles
              can take several minutes before the first file appears.
            </div>
          )}
          {terminal && status === "completed" && (
            <a className="btn btn-primary btn-sm self-start" href={jobZipUrl(jobId)}>
              ⬇ Download all (.zip)
            </a>
          )}
          {terminal && status === "failed" && finalReason && (
            <div className="alert alert-error flex-col items-start gap-1 py-2 text-sm">
              {finalReason.reason === "missing-cookies" ? (
                <>
                  No cookies configured for this platform.{" "}
                  <a className="link" href="/settings">
                    Add them in Settings
                  </a>
                  .
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
