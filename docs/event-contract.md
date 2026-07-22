# SSE event contract

This is the wire format agreed between the FastAPI **backend** (`gallery_dl_web.gallerydl.worker`
→ `JobManager` → `GET /api/jobs/{id}/events`) and the **Next.js frontend** (`JobProgress.tsx`).
Both sides must honor it; the TS mirror lives in `frontend/src/lib/events.ts`.

## Transport

- **Worker → JobManager**: the worker writes one JSON object per line to stdout (newline-terminated,
  flushed immediately). `JobManager` reads each line, parses it, and fans it to SSE subscribers.
- **Backend → Frontend**: Server-Sent Events. Each event is emitted as
  `event: <type>\ndata: <json>\n\n`. The frontend uses `EventSource` with typed listeners.

## Event types

| `type`      | When                                          | Key fields                                              |
| ----------- | --------------------------------------------- | ------------------------------------------------------- |
| `queued`    | Job accepted, before the worker spawns        | `job_id`, `url`                                         |
| `started`   | Worker process spawned, gallery-dl running    | `job_id`, `url`                                         |
| `prepare`   | gallery-dl resolved a file, about to fetch    | `filename`, `url`                                       |
| `file`      | A file was handled                            | `event` (`downloaded`\|`skipped`), `path`, `filename`, `bytes`? |
| `progress`  | Running counts (manager-emitted, job-level/monotonic across retries), after each `file` | `downloaded`, `skipped`, `failed` |
| `heartbeat` | Worker liveness while gallery-dl is silent (non-terminal) | `beat`, `elapsed`                    |
| `stalled`   | No **file** event within the progress deadline (non-terminal) | `attempt`, `threshold`, `phase` (`warmup` \| `download`), `since_last_file`? |
| `retrying`  | The stalled/exit worker was killed and a fresh one will spawn (non-terminal) | `attempt`, `reason` (`stalled` \| `worker-exited`) |
| `error`     | A recoverable or fatal error                  | `message`, `kind`, `fatal` (bool)                       |
| `completed` | **Terminal.** Worker exited status 0          | `exit_status`, `downloaded`, `skipped`, `reason`        |
| `failed`    | **Terminal.** Worker exited non-zero, or retries exhausted (`reason`: `stalled` \| `no-progress` \| `worker-crash` \| `missing-cookies` \| `downloads-dir-unwritable`) | `exit_status`, `reason`, `message`? |
| `ping`      | sse-starlette keepalive (15 s)                | `{}`                                                    |
| `end`       | Synthetic terminal sentinel from the SSE route | `{ "terminal": true }`                                |

## Rules

1. **Exactly one terminal event** (`completed` or `failed`) is always emitted last. `stalled`,
   `retrying` and `heartbeat` are **non-terminal** — the manager emits them around a kill+respawn
   (or continuously), then the final `completed`/`failed`.
2. `progress` is **manager-emitted** (job-level, monotonic across retries), after each `file` event;
   the worker's own per-attempt `progress` events are dropped.
3. All events carry `ts` (unix float) and `job_id` (except `ping`/`end`).
4. `bytes` is `null` when gallery-dl doesn't expose the size; otherwise the on-disk file size.
5. `fatal: true` on an `error` means the job will terminate; `fatal: false` is informational
   (e.g. a single 429 backoff that recovered).
6. **Events never carry cookie values** — only filenames, paths, and counts.
7. **Two independent deadlines** guard a job (see `jobs/manager.py`):
   - *liveness* — no line at all, not even a `heartbeat`, within `STALL_LIVENESS_SECONDS` means the
     worker process is wedged (e.g. blocked writing to a full stderr pipe).
   - *progress* — no `file` event within the threshold. Before the first file that threshold is the
     **warm-up** budget (`STALL_WARMUP_SECONDS`), because gallery-dl is silent for minutes while it
     enumerates a profile — Instagram alone sleeps 6-12 s per paginated request. After the first
     file it is `clamp(floor*backoff**attempt, multiplier*avg_inter_file, cap)`.
   `heartbeat` deliberately resets only the liveness clock, never the progress clock.
8. A warm-up timeout fails with `reason: no-progress` (not `stalled`) and gets its own, smaller
   retry budget: with zero files fetched the archive has nothing to resume, so a retry just repeats
   the same slow enumeration.
9. `failed.message` carries the tail of the worker's stderr when there is one — that is where
   gallery-dl's real error text (auth wall, permission denied, rate limit) appears.

## Example stream

```jsonl
{"type":"queued","job_id":"191f…","url":"https://www.instagram.com/p/Cxxx/","ts":1721570000.0}
{"type":"started","job_id":"191f…","url":"https://www.instagram.com/p/Cxxx/","ts":1721570000.4}
{"type":"prepare","filename":"2024-07-21_Cxxx.jpg","url":"…","ts":1721570001.2}
{"type":"file","event":"downloaded","path":"/data/downloads/instagram/user/2024-07-21_Cxxx.jpg","filename":"2024-07-21_Cxxx.jpg","bytes":248311,"ts":1721570002.1}
{"type":"progress","downloaded":1,"skipped":0,"failed":0,"ts":1721570002.1}
{"type":"completed","job_id":"191f…","exit_status":0,"downloaded":1,"skipped":0,"reason":"ok","ts":1721570002.2}
```

## Exit-status → terminal mapping (gallery-dl bitmask)

| `status &` | meaning        | terminal                                            |
| ---------- | -------------- | --------------------------------------------------- |
| `0`        | success        | `completed`                                         |
| `1`        | error          | `failed` reason `error`                             |
| `4`        | download failed | `failed` reason `dl-failed` (some files may exist) |
| `8`        | all skipped    | `completed` reason `all-skipped`                    |
| `64`       | no extractor   | `failed` reason `no-extractor` (unsupported URL)    |
| `128`      | OS error       | `failed` reason `os-error`                          |

A worker process exit code of `2` (vs gallery-dl status) means the **worker itself** crashed before
producing a terminal event; the backend synthesizes a `failed`/`worker-crash` event.
