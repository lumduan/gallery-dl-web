// Mirror of the backend SSE event contract (see docs/event-contract.md).
export type JobEventType =
  | "queued"
  | "started"
  | "prepare"
  | "file"
  | "progress"
  | "heartbeat"
  | "stalled"
  | "retrying"
  | "paused"
  | "resumed"
  | "error"
  | "completed"
  | "failed"
  | "cancelled"
  | "ping"
  | "end";

/** Every event type the SSE stream can deliver — used to register EventSource listeners. */
export const JOB_EVENT_TYPES: JobEventType[] = [
  "queued",
  "started",
  "prepare",
  "file",
  "progress",
  "heartbeat",
  "stalled",
  "retrying",
  "paused",
  "resumed",
  "error",
  "completed",
  "failed",
  "cancelled",
];

export interface JobEvent {
  type: JobEventType | string;
  job_id?: string;
  url?: string;
  filename?: string;
  path?: string | null;
  bytes?: number | null;
  event?: "downloaded" | "skipped";
  downloaded?: number;
  skipped?: number;
  failed?: number;
  message?: string;
  kind?: string;
  fatal?: boolean;
  reason?: string;
  /** failed/rate-limited only: a URL the platform gave to resume from (gallery-dl's &setextract). */
  resume_url?: string;
  exit_status?: number;
  attempt?: number;
  threshold?: number;
  since_last_file?: number | null;
  /** "warmup" = timed out before the first file (still enumerating); "download" = mid-transfer. */
  phase?: "warmup" | "download";
  /** heartbeat only: beat counter and seconds since the worker started. */
  beat?: number;
  elapsed?: number;
  /** resumed only: how long the job was paused, in seconds. */
  paused_for?: number;
  ts?: number;
}

export function isTerminal(type: string): boolean {
  // `cancelled` is terminal too — a deliberate stop, not a failure.
  return type === "completed" || type === "failed" || type === "cancelled";
}
