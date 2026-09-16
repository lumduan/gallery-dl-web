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
| `pushback`  | A platform pushback signature matched (non-terminal) | `platform`, `tier` (`throttle`\|`terminal`), `rule`, `delay`, `requests`, `body` |
| `pacing-telemetry` | The worker was SIGTERMed and flushed its request ring buffer (non-terminal) | `reason` (`terminated`), `pacing_telemetry` |
| `stalled`   | No **file** event within the progress deadline (non-terminal) | `attempt`, `threshold`, `phase` (`warmup` \| `download`), `since_last_file`? |
| `retrying`  | The stalled/exit worker was killed and a fresh one will spawn (non-terminal) | `attempt`, `reason` (`stalled` \| `worker-exited`) |
| `paused`    | Operator paused the job; worker SIGSTOPed (non-terminal) | `downloaded`, `skipped`                |
| `resumed`   | Operator resumed it; worker SIGCONTed after re-acquiring a slot (non-terminal) | `paused_for`, `downloaded`, `skipped` |
| `error`     | A recoverable or fatal error                  | `message`, `kind`, `fatal` (bool)                       |
| `completed` | **Terminal.** Worker exited status 0          | `exit_status`, `downloaded`, `skipped`, `reason`, `pacing_telemetry`? |
| `failed`    | **Terminal.** Worker exited non-zero, or retries exhausted (`reason`: `stalled` \| `no-progress` \| `rate-limited` \| `login-required` \| `worker-crash` \| `downloads-dir-unwritable` \| a gallery-dl reason such as `dl-failed`) | `exit_status`, `reason`, `message`?, `resume_url`?, `pacing_telemetry`? |
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

    One shape gets its own message under the same reason: **Facebook's content-free profile
    shell**. It answers HTTP 200 with none of the markers gallery-dl parses, which upstream turns
    into `KeyError: 'set_id'` plus an invitation to file a gallery-dl bug.
    `gallerydl/upstream_patches.py` converts it to gallery-dl's own `AuthRequired`, and
    `errors.py:detect_empty_profile` classifies it — checked *after* the rate limit and *before*
    the generic wall, because its text matches both and only it names the other possibility: a
    profile that no longer exists is served the identical page, so the two cannot be told apart.
    That message deliberately carries no "un-tick anonymous" advice, since the same text is shown
    to a cookied run that was refused; the frontend adds that line from `JobSummary.anonymous`.
    The detector matches the crash text as well as the patched wording, so the reason survives the
    patch failing to install.
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
    ⚠️ **A media download never counts toward the clean streak.** Downloads share the extractor's
    session so they reach the pacer's response hook, but they bypass `Extractor.request` — so they
    are neither paced nor counted toward the ramp, while outnumbering extractor requests ~30:1 on
    Instagram. Counting them as clean decayed a ceiling-level penalty back to the floor inside a
    single page, which meant `adaptive` behaved as `fixed(floor)` in exactly the regime it exists
    to protect. Their *status* is still judged — a CDN 429 on an image is real pushback.
12b. **`pacing_telemetry` is the post-mortem record**, not a live feed. The worker keeps a bounded
    ring buffer (last 50 observed requests) and attaches it **once**, to its own terminal event —
    or, when the manager SIGTERMs it on the stall-kill path, flushes it as a standalone
    **non-terminal** `pacing-telemetry` event, because a terminal event from the worker there would
    race the manager's synthesized one and break rule 1. It is deliberately not a per-request event
    type, for the deque reason in rule 12. Each entry is
    `{i, delay, floor, ceiling, status, url, content_type, body, streamed, classified, rule}`.
    Per rule 6 it carries **no cookie values, no request headers, and no URL query string** — `url`
    is host + path only, because Instagram signs media URLs in the query; `body` is a redacted
    500-byte prefix captured only for JSON/HTML responses, and never read from a streamed one.
    The key is absent when nothing was observed, and in `fixed` mode (no pacer is installed).
12c. **`pushback` reports WHY, where `pacing` reports WHAT.** `pacing` says the delay moved and is
    rate-limited to one per 5 s — right for a ramp climbing in millisecond steps, wrong for "the
    platform just threw us out". A pushback is rare by nature and is emitted every time a signature
    matches, though it still shares `pacing`'s 200-event budget for the deque reason in rule 12.
    `tier` is `throttle` (transient — back off and keep going) or `terminal` (stop; retrying into a
    checkpoint extends the block). `rule` names the signature, e.g. `ig-redirect-root`.
    ⚠️ **Instagram's block is a 302 to the bare home page**, captured 2026-08-26 after 859 downloads
    and 885 s. It is not a 429 and not an HTTP 200 carrying `{"status": "fail"}`. Before the
    signature table, all 50 requests in that run's ring buffer — the fatal redirect included —
    classified `clean`, which is why the back-off never engaged.
    `body` is a redacted ≤200-byte prefix. Per rule 6 it carries no cookie values, no request
    headers and no URL query string; `pushback` carries no URL at all.

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
| `16`       | auth required  | `failed` reason `login-required`                    |
| `4`        | download failed | `failed` reason `dl-failed` (some files may exist) |
| `8`        | all skipped    | `completed` reason `all-skipped`                    |
| `64`       | no extractor   | `failed` reason `no-extractor` (unsupported URL)    |
| `128`      | OS error       | `failed` reason `os-error`                          |

The ladder is `64 > 128 > 16 > 4 > 1 > 8`, and **16 above 4 is deliberate**: `Extractor.status`
accumulates `4` from any fatal `HttpError`/`NotFoundError` earlier in the run and `Job.run`'s
`finally` ORs it in, so `4 | 16` is the ordinary shape of an auth failure and a lower placement
would almost never fire. The mapping is a backstop — when the stderr tail survives,
`_annotate_failure` reaches the same reason from the message text.

A worker process exit code of `2` (vs gallery-dl status) means the **worker itself** crashed before
producing a terminal event; the backend synthesizes a `failed`/`worker-crash` event.
