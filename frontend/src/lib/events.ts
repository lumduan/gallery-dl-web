// Mirror of the backend SSE event contract (see docs/event-contract.md).
export type JobEventType =
  | "queued"
  | "started"
  | "prepare"
  | "file"
  | "progress"
  | "stalled"
  | "retrying"
  | "error"
  | "completed"
  | "failed"
  | "ping"
  | "end";

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
  exit_status?: number;
  attempt?: number;
  threshold?: number;
  since_last_file?: number | null;
  ts?: number;
}

export function isTerminal(type: string): boolean {
  return type === "completed" || type === "failed";
}
