// Mirror of the backend SSE event contract (see docs/event-contract.md).
export type JobEventType =
  | "queued"
  | "started"
  | "prepare"
  | "file"
  | "progress"
  | "heartbeat"
  | "pacing"
  | "pacing-telemetry"
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
  "pacing",
  "pacing-telemetry",
  "stalled",
  "retrying",
  "paused",
  "resumed",
  "error",
  "completed",
  "failed",
  "cancelled",
];

/**
 * One observed request, as the pacer saw it. Post-mortem evidence, not a live feed — the worker
 * keeps the last 50 and flushes them once (see docs/event-contract.md rule 12b).
 *
 * Carries no cookie values, no request headers and no URL query string: `url` is host + path only
 * (Instagram signs media URLs in the query) and `body` is a redacted 500-byte prefix, captured only
 * for JSON/HTML and never read from a streamed response.
 */
export interface PacingTelemetryEntry {
  /** Monotonic request index within the job; survives ring-buffer eviction. */
  i: number;
  /** Seconds: the delay in force, the ramped floor, and the back-off ceiling at that moment. */
  delay: number;
  floor: number;
  ceiling: number;
  status: number | null;
  url: string;
  content_type: string;
  body: string;
  /** True for media downloads — their body is never read and they never count as a clean request. */
  streamed: boolean;
  /** `clean`, or the pacing reason that matched. */
  classified: string;
  /** The named signature that matched, once the signature table exists. */
  rule: string | null;
}

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
  /**
   * On `failed`: `stalled` | `no-progress` | `rate-limited` | `login-required` | `worker-crash` |
   * `downloads-dir-unwritable` | a gallery-dl reason such as `dl-failed`.
   * `login-required` is what an anonymous (cookie-free) run hits on private or session-walled
   * content — the operator's action is to add cookies in Settings.
   */
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
  /**
   * pacing only: the adaptive per-request delay changed, in seconds. Emitted on change only
   * (and rate-limited), never on every request. `reason` is why it moved — `ramp` and
   * `recovered` are routine, anything else means the platform pushed back.
   */
  delay?: number;
  previous?: number;
  requests?: number;
  platform?: string;
  /** resumed only: how long the job was paused, in seconds. */
  paused_for?: number;
  /**
   * On a terminal event, or on a standalone `pacing-telemetry` event when the worker was killed:
   * the last requests the pacer observed. Absent when nothing was observed or pacing was `fixed`.
   */
  pacing_telemetry?: PacingTelemetryEntry[];
  ts?: number;
}

export function isTerminal(type: string): boolean {
  // `cancelled` is terminal too — a deliberate stop, not a failure.
  return type === "completed" || type === "failed" || type === "cancelled";
}
