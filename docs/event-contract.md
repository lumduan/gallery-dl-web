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
| `pacing`    | The adaptive request delay changed (non-terminal) | `platform`, `delay`, `previous`, `reason`, `requests` |
| `stalled`   | No **file** event within the progress deadline (non-terminal) | `attempt`, `threshold`, `phase` (`warmup` \| `download`), `since_last_file`? |
| `retrying`  | The stalled/exit worker was killed and a fresh one will spawn (non-terminal) | `attempt`, `reason` (`stalled` \| `worker-exited`) |
| `paused`    | Operator paused the job; worker SIGSTOPed (non-terminal) | `downloaded`, `skipped`                |
| `resumed`   | Operator resumed it; worker SIGCONTed after re-acquiring a slot (non-terminal) | `paused_for`, `downloaded`, `skipped` |
| `error`     | A recoverable or fatal error                  | `message`, `kind`, `fatal` (bool)                       |
| `completed` | **Terminal.** Worker exited status 0          | `exit_status`, `downloaded`, `skipped`, `reason`        |
| `failed`    | **Terminal.** Worker exited non-zero, or retries exhausted (`reason`: `stalled` \| `no-progress` \| `rate-limited` \| `login-required` \| `worker-crash` \| `downloads-dir-unwritable` \| a gallery-dl reason such as `dl-failed`) | `exit_status`, `reason`, `message`?, `resume_url`? |
| `cancelled` | **Terminal.** Operator stopped the job (`reason`: `cancelled`) | `reason`, `message`, `downloaded`, `skipped` |
| `ping`      | sse-starlette keepalive (15 s)                | `{}`                                                    |
| `end`       | Synthetic terminal sentinel from the SSE route | `{ "terminal": true }`                                |

## Rules

1. **Exactly one terminal event** (`completed`, `failed`, or `cancelled`) is always emitted last.
   `stalled`, `retrying`, `heartbeat`, `paused` and `resumed` are **non-terminal** — the manager
   emits them around a kill+respawn, a pause, or continuously, then the single terminal event.
   `cancelled` is terminal but is **not** a failure: it means the operator stopped the job, the
   files already fetched were kept, and the UI must not present it as an error.
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
   `heartbeat` deliberately resets only the liveness clock, never the progress clock — and
   **neither does `pacing`**: sleeping is the opposite of progress, and treating it as activity
   would defeat stall detection exactly the way a heartbeat would.
8. A warm-up timeout fails with `reason: no-progress` (not `stalled`) and gets its own, smaller
   retry budget: with zero files fetched the archive has nothing to resume, so a retry just repeats
   the same slow enumeration.
9. `failed.message` carries the tail of the worker's stderr when there is one — that is where
   gallery-dl's real error text (auth wall, permission denied) appears.
10. A platform rate limit is promoted to `reason: rate-limited` with a plain-language `message`
    instead of the raw traceback: it is not an application error, and the only useful action is to
    wait — retrying extends the block. Detected from stderr by
    `gallerydl/errors.py:detect_rate_limit`, and it overrides the reason on both the worker's own
    terminal event and a manager-synthesized stall. When the platform supplies a resume point
    (gallery-dl's `&setextract` URL for a Facebook set) it is passed through as `resume_url`.
11. **A job with no cookies runs anonymously rather than being refused.** There is no
    `missing-cookies` reason — it was retired. gallery-dl reaches public content logged-out, so the
    manager falls back to an anonymous run (and an operator can force one with `options.anonymous`
    even when cookies *are* stored). If the content turns out to need a session, the failure says
    so: stderr matching an auth wall is promoted to `reason: login-required` with a plain-language
    `message` pointing at Settings, by `gallerydl/errors.py:detect_login_wall`. That match covers
    gallery-dl's `AuthRequired` prose, a urllib3 `… HTTP/1.1" 401 <len>` debug line, and Instagram's
    HTTP-200-plus-`require_login` refusal — the prose alone misses the common cases. The detector
    also takes `anonymous`, which widens it to `NotFoundError: Requested user could not be found`:
    that text is a failed username lookup, which is an auth wall with no session but can be a
    genuinely deleted account with one. A **rate limit is classified first** — Facebook's block page
    carries login-ish wording, and there the correct advice is to wait, not to re-export cookies.
    `JobSummary.anonymous` reports which mode a job actually ran in.
12. **Pacing is adaptive by default, and `pacing` reports it.** Both platforms are rate-limited,
    but Facebook fetches one full HTML page *per photo* where Instagram gets ~30 posts per JSON
    request, so a fixed delay large enough to be safe on a long Facebook run makes every short one
    needlessly slow. In `adaptive` mode the worker starts at the configured floor and slows down
    only on evidence — a 429, a 403/503, a login redirect, Facebook's block page, or gallery-dl's
    own warnings — then decays back after a clean streak. The floor additionally rises with the
    number of requests already made, because the one block ever observed came ~767 images into a
    single run. A `pacing` event is emitted **only when the delay changes**, at most every 5 s and
    at most 200 times per job: `JobState.events` is a bounded deque that `media_paths()` and the
    zip route read `file` events back out of, so a chatty event type would silently truncate a
    job's downloads. `delay` and `previous` are seconds; `reason` is one of `ramp`, `recovered`,
    `http-429`, `http-403`, `http-503`, `http-900`, `login-redirect`, `block-page`, `challenge`,
    `rate-limited`, `no-download-url`, `request-error`.
    ⚠️ Back-off lowers the odds of *reaching* a block; it cannot recover from one. gallery-dl
    aborts the run the moment it sees Facebook's block page, and that outcome is still reported by
    rule 10's `rate-limited` classification.
13. **Pause is a real process suspension**, driven by `POST /api/jobs/{id}/pause`. The manager
    SIGSTOPs the worker, so gallery-dl keeps its place in the profile walk and no `heartbeat`
    arrives until it resumes. The concurrency slot is handed back — that is the point, a waiting
    profile starts immediately — and re-acquired on resume, so a resumed job can legitimately sit
    at `queued` (with `started_at` already set) before its `resumed` event. Paused wall-time is
    subtracted from every stall clock, so a pause can never be reported as a stall.

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
