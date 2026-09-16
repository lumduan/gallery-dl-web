# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Two-service web app wrapping `gallery-dl` to download Instagram & Facebook images.

## Stack
- **Backend** (`backend/`, Python 3.12): FastAPI + `gallery-dl` + sse-starlette + Pillow. uv,
  src-layout, hatchling, ruff (E/F/I/UP/B/SIM, line 100), mypy strict on `src`, pytest ≥80% coverage.
- **Frontend** (`frontend/`, Node 24): Next.js 16 + React 19 + Tailwind v4 + DaisyUI 5.
- **Containers**: multi-stage Dockerfiles, non-root UID 1001; `docker-compose.yml` (prod) +
  `docker-compose.dev.yml` (dev, **standalone**). Images → `ghcr.io/lumduan/gallery-dl-web/{backend,frontend}`.

## Architecture in one paragraph
`POST /api/jobs` → `JobManager` spawns a subprocess `python -m gallery_dl_web.gallerydl.worker`, sends
its config (incl. cookies) over **STDIN**, and streams the worker's JSON-lines stdout to SSE
subscribers. One process per job isolates gallery-dl's global `config` state. Cookies never touch
argv, disk, logs, or API responses (repo is public). The Next.js frontend proxies `/api/*` to the
backend via a catch-all route (`src/app/api/[...path]/route.ts`) that reads `BACKEND_URL` at request
time — NOT `next.config` rewrites, which bake the destination in at build time. After a job reaches a
terminal state the manager reconciles the affected profile's `metadata.json`, which is what the
`/profiles` gallery UI reads.

## Commands
Backend (`cd backend`): `uv sync --all-groups` · `uv run python -m gallery_dl_web` (uvicorn :8000) ·
`uv run pytest` · `uv run ruff check .` · `uv run ruff format --check .` · `uv run mypy src`.
Frontend (`cd frontend`): `npm install` · `npm run dev` · `npm run build` ·
`npm run typecheck` · `npm run lint`.

Single test / fast loop (the `--cov-fail-under=80` gate in `pyproject.toml` fails any narrow run,
so pass `--no-cov`):
```bash
uv run pytest tests/jobs/test_manager.py -q --no-cov
uv run pytest tests/jobs/test_stall.py::test_stall_retries_then_fails -q --no-cov
```
CI (`.github/workflows/ci.yml`) runs ruff check **and** `ruff format --check`, mypy, pytest with
coverage, then frontend lint + typecheck + build. Run those five before pushing.

Compose:
```bash
docker compose -f docker-compose.dev.yml up    # dev — standalone, NOT merged with the prod file
docker compose up -d                           # prod
```
Merging the dev overlay with the prod file is wrong: the overlay's `image: node:24-alpine` combines
with prod's `build: ./frontend`, so compose builds the production frontend and tags it
`node:24-alpine`.

`docker-compose.hostdir.yml` is a separate, opt-in overlay that bind-mounts a host/NAS directory for
media. Setting `DOWNLOADS_DIR` alone does **nothing** — the path also has to exist inside the
container. Enable both together in `.env`:
`DOWNLOADS_DIR=/mnt/...` plus `COMPOSE_FILE=docker-compose.yml:docker-compose.hostdir.yml`
(compose reads `COMPOSE_FILE` automatically, so `docker compose up -d` is unchanged).

⚠️ **On an autofs/NFS path that overlay does not survive a reboot, and the restart policy will not
save it.** Docker's restore pass runs seconds after boot; `mkdir` on an untriggered autofs
mountpoint returns `ENODEV`, so the container never starts (`… mkdir …: no such device`). Because it
never reached a running state there is no exit for `unless-stopped` to count — `RestartCount` stays
0 and the stack stays down indefinitely. This is a **host** problem, not an app one: the fix is a
`docker-nas-mount-reconcile.service` oneshot ordered *after* `docker.service` that waits for the
mounts and then `docker start`s any container whose start failed with a mount error. Ordering it
after Docker is deliberate — making Docker wait on the NAS would let a NAS outage hold every
unrelated container on the host hostage.

## Key files
- `backend/src/gallery_dl_web/gallerydl/worker.py` — subprocess entry; the load-bearing contract.
- `backend/src/gallery_dl_web/jobs/manager.py` — asyncio orchestrator (spawn/fan-out/replay/stall-retry/GC).
- `backend/src/gallery_dl_web/gallerydl/config_builder.py` — pure payload→`config.set` translator.
- `backend/src/gallery_dl_web/gallerydl/pacing.py` — the adaptive request pacer + its three patches.
- `backend/src/gallery_dl_web/gallerydl/upstream_patches.py` — workarounds for gallery-dl bugs.
- `backend/src/gallery_dl_web/pacing/store.py` — operator pacing overrides (`<data_dir>/pacing.json`).
- `backend/src/gallery_dl_web/files/index.py` — the downloads listing, off the event loop.
- `backend/src/gallery_dl_web/api/routes_jobs.py` — SSE endpoint + zip.
- `backend/src/gallery_dl_web/profiles/store.py` — per-profile `metadata.json` reconciliation.
- `frontend/src/app/jobs/[id]/page.tsx` + `components/JobProgress.tsx` — SSE consumer.
- `frontend/src/components/QueueList.tsx` — the `/queue` dashboard (polls `/api/jobs`, not SSE:
  one poll carries every row's counters, where an EventSource per row would be N streams).
- `docs/event-contract.md` — the shared SSE schema (change both sides together;
  `frontend/src/lib/events.ts` is its TS mirror).

## Invariants worth knowing before you edit

**Worker progress hooks are a gallery-dl *postprocessor*, not `Job.register_hooks`.** Postprocessors
are extractor-level config, so they also fire for the child jobs Facebook/Instagram profile
extraction spawns; `register_hooks` binds to one `DownloadJob` and silently misses child downloads.
The callbacks are registered under the single-segment module name `gallery_dl_web_hooks` in
`sys.modules` because gallery-dl resolves `function` via `__import__`, which returns the *top-level*
package for a dotted name — a spec like `gallery_dl_web.gallerydl.worker:on_file` resolves to the
wrong module. Don't "clean up" that indirection.

**Worker stdout is the event channel.** Only JSON lines go to stdout; gallery-dl's own logging and
interactive output are forced to stderr / `mode: null`. Anything that prints to stdout in the worker
corrupts the stream.

**The manager owns job-level counters.** It drops the worker's `progress` events, re-emits its own
(monotonic across retries), and overwrites `downloaded`/`skipped` on terminal events. Per-attempt
counts from the worker are not authoritative.

**Exactly one terminal event per job** (`completed` | `failed` | `cancelled`), always. The manager
synthesizes one if the worker dies silently or exhausts retries; `stalled`/`retrying`/`heartbeat`/
`paused`/`resumed` are non-terminal. `cancelled` is terminal but is **not** a failure — the operator
asked for it, the fetched files are kept, and the UI must not render it red.

**Pause is a real SIGSTOP, and the read loop — not `pause()` — owns the concurrency slot.**
`pause()` signals the worker and flips the status; `_await_resume` releases the slot when the loop
actually parks and re-acquires it before SIGCONT. Releasing in `pause()` looks equivalent but
breaks a real race: a resume arriving before the loop noticed the pause finds the slot already gone
and never re-acquires it, so the job runs outside `max_concurrent_jobs` — and the worker stays
suspended with nothing left to continue it. That is why the loop enters `_await_resume` on
`paused_at is not None`, not on `pause_requested`. On resume, `first_file_ts` **and** `last_file_ts`
shift by the same amount so `_stall_threshold`'s `avg = (last-first)/(n-1)` is unchanged.

**The read loop's wait is `asyncio.wait({read, control})`, and the pending `readline()` survives
across iterations.** Cancelling a partially-consumed read would drop whatever the worker already
wrote. `state.control_event` is cleared at the *top* of the loop, before the flags are tested, so an
action landing mid-iteration leaves the event set and the next waiter returns immediately instead of
being swallowed. `_kill_worker` sends SIGCONT before SIGTERM — a stopped process handles no signals
and `proc.wait()` would never return.

**Stall detection is two independent deadlines, and conflating them breaks downloads.**
- *liveness* — no line at all on worker stdout, not even a `heartbeat`, within
  `stall_liveness_seconds`: the process is wedged, not slow.
- *progress* — no `prepare`/`file` within `_stall_threshold`. Before any activity that is the
  **warm-up** budget (`stall_warmup_seconds`, 600 s), because gallery-dl is silent for minutes
  while enumerating a profile; after activity starts it is
  `clamp(floor*backoff^attempt, multiplier*avg_inter_file, cap)`.

Both were originally one "any output within 90 s" deadline, which killed healthy jobs mid-walk and
then retried them from zero — with no files fetched the archive has nothing to resume, so it could
never converge. Two things that look like they shouldn't matter, do: **`prepare` counts as
progress** (a live run produced 90 prepares in 60 s with zero `file` events), and **`heartbeat`
must NOT reset the progress clock** (it would defeat stall detection entirely).

**Worker stderr must be drained.** `spawn_worker` opens it as a pipe; `JobManager._drain_stderr`
consumes it continuously. Undrained, ~64 KB blocks the worker mid-write — its stdout goes silent
and the stall detector reports a phantom stall. The tail is attached to `failed.message`, which is
the only place gallery-dl's real error text (auth wall, rate limit, permission denied) ever appears.

**Cookies are optional; a job with none runs anonymously.** There is no `missing-cookies` failure
any more — gallery-dl reaches public content logged-out, so `_run_job` falls back to an anonymous
run, and `options.anonymous` forces one even when cookies are stored. The flag is a **top-level
payload key** next to `cookies` (the manager `pop`s it out of `options`, which `config_builder`
only reads for keys in `_PLATFORM_DEFAULTS`), and `cookies` is then `None`. Setting
`config.set(("extractor", <platform>), "cookies", None)` is deliberate and safe: gallery-dl's
`Extractor._init_cookies` is guarded by `if cookies := self.config("cookies")`, so a falsy value
no-ops rather than erroring, and the explicit call keeps the "cookies are set *before* the defaults
loop, so an `options` key can never overwrite them" invariant intact.

Two anonymous-only Instagram adjustments, neither of which applies to Facebook: `api` is set to
`graphql` (the default REST `/api/v1/*` endpoints mostly 401 logged-out), and `stories` /
`highlights` / `saved` / `collection` are stripped from `include` — logged-out these raise
`AbortExtraction` and kill the whole walk instead of merely returning nothing.

**`include` is resolved exactly once, before the defaults loop** (`_resolve_include`), because the
avatar block appends to it. It used to be derived twice from raw `options`; leave it that way and
the avatar append silently re-introduces the categories anonymous mode just filtered out.

**Instagram's block is a 302 to the bare home page, and pushback lives in a signature table.**
Captured 2026-08-26: a run at `adaptive 4-30` died after 859 downloads / 885 s with
`AbortExtraction: HTTP redirect to home page`. It is **not** a 429 and **not** an HTTP 200 carrying
`{"status": "fail"}` — the hypothesis that shaped the original bug report. All 50 requests in that
run's ring buffer, the fatal redirect included, classified `clean`, because the only URL markers
were `/accounts/login` and Facebook's while the redirect target is the bare domain root. That is why
the back-off never engaged: `delay == floor` on every observation, the volume ramp carrying the
whole run alone.

`gallerydl/signatures.py` holds the rules as **data** — a per-platform `(name, tier, predicate)`
table — so the live sensor and the reported reason cannot drift, and so a Meta wording change is a
one-line edit. Four tiers, and the split is the point: `THROTTLE` backs off and keeps going,
`TERMINAL` stops (retrying into a checkpoint extends the block and gallery-dl has already raised
`AbortExtraction` by then), `CLEAN` may advance the streak, and **`UNKNOWN` must never advance it** —
"we could not tell" is not evidence of health, and treating it as such was half of why the old
controller could not react. Rules marked OBSERVED came from that capture; the rest are carried
because `errors.py` already matches the same wording on stderr, which keeps the two sensors aligned.
An unrecognised platform runs **every** table rather than none: failing toward detection, and safe
because the rules are host- and marker-scoped.

**A login wall is classified, and rate-limiting wins the tie.** `errors.py:detect_login_wall`
matches gallery-dl's own `AuthRequired` wording and `_annotate_failure` promotes it to
`reason: login-required`. It is checked *after* `detect_rate_limit`: Facebook's block page also
reads login-ish, and there the right advice is "wait", not "re-export your cookies" — retrying
extends the block. Keep that order.

**Match the text the platform actually returns, not the text gallery-dl's source defines.** The
first version of that classifier matched only the `AuthRequired` prose and missed the most common
real failure. Instagram never emits the prose: an anonymous profile job dies on a urllib3 debug
line reading `… HTTP/1.1" 401 42` — status then *content length*, the word "Unauthorized" nowhere —
and Instagram also refuses with HTTP **200** plus `"require_login":true` in the body. Both are
matched now. Note the 401 reaches stderr at all only because `output.initialize_logging` sets the
root logger to `NOTSET`, so the worker's own `basicConfig` handler prints gallery-dl's debug
records too; that is load-bearing for the match, and there is a second, independent rule so the
classification survives if it ever changes.

**`detect_login_wall` takes `anonymous`, because identical stderr means different things.**
gallery-dl's `user_by_screen_name` tries each `user-strategy`, swallows every real exception into a
debug line, and raises one generic `NotFoundError: Requested user could not be found` — so an auth
wall and a deleted account are textually identical. With no session the ambiguity resolves (every
anonymous lookup path is walled: topsearch 401, and the logged-out profile page no longer embeds
`"profile_id"`), so that text counts as a login wall *only* when `anonymous` is set. With cookies it
stays unmatched, since sending that operator to Settings would be wrong.

**`gallerydl/upstream_patches.py` works around a gallery-dl bug, and it deliberately does NOT do
what upstream meant.** `FacebookExtractor._extract_profile_page` injects `set_id` only on its
success branch, so both failure exits return a bare `{}`, and `FacebookPhotosExtractor.items`
immediately subscripts `["set_id"]` on it — `KeyError: 'set_id'`, reported to the operator as
gallery-dl's "report this issue on codeberg". Identical in 1.32.9 and 1.32.12, so there is nothing
to upgrade to. The line *after* the crash site is `if not set_id: return iter(())`, so upstream
intended a silent empty result; restoring that would give a total failure `status 0` / `reason
"ok"` / zero files — a **silent success**, which for a profile downloader is worse than the crash.
We raise `AuthRequired` instead, in the exact shape `facebook.py:363` already uses for the sibling
failure, so `errors.py` classifies it with no new pattern and `job.py` logs one clean line instead
of a traceback. Don't "fix" it back.

Raising is also what un-poisons the avatar: `Extractor.cache` keys on the profile name alone — the
`set_id=True/False` argument is not part of the key, and `_exp=0` memoizes for the life of the
process — so the `{}` from `/photos_by` was being handed to `FacebookAvatarExtractor`, which is why
a failed run logged the avatar finding "No results" *with no request of its own*. Nothing is
memoized when the call raises. It costs one extra pair of requests on a genuinely walled profile;
that is the price of the avatar still working on one that is only partly walled.

**Logged out, a walled Facebook profile and a nonexistent one are indistinguishable.** Probed live
2026-09-16: both return HTTP 200 and the same ~326 KB content-free shell, and neither carries the
`>Page Not Found</title>` marker gallery-dl looks for, so that branch is effectively dead. A Page
(`/facebook`) and other personal profiles (`/zuck`) still render anonymously, so this is per-profile,
not a global wall — the memory note that "cookie-free downloads work on FB" is still true, just not
universally. Both the exception text and `EMPTY_PROFILE_MESSAGE` therefore state the **observation**
("a page with none of the profile data gallery-dl reads"), never a conclusion: if Facebook changes
its markup the markers stop matching for *every* profile, and a message concluding "your cookies are
bad" would send every operator off to re-export a perfectly good session instead of reporting an
upstream break.

**Exit bit 16 must outrank bit 4 in `map_exit_status`.** `AuthRequired`/`AuthorizationError`/
`AuthenticationError` all carry code 16, but `Extractor.status` independently accumulates 4 from any
fatal `HttpError`/`NotFoundError` earlier in the run and `Job.run`'s `finally` ORs it in — so
`4 | 16` is the ordinary shape of an auth failure, and 16 placed below 4 would almost never fire.

**Tests must never spawn a real worker, never make a real request, and never leave gallery-dl
patched.** `tests/conftest.py`'s autouse `_no_real_spawn` covers the manager; `tests/gallerydl/conftest.py`
adds the worker-side siblings, because that code runs *in-process*: `_no_real_http` makes
`HTTPAdapter.send` raise, and `_pristine_extractor` asserts after every test that `Extractor.request`,
`_init_session`, `FacebookExtractor._extract_profile_page` and the root log handlers were restored —
`pacing.install` and `upstream_patches.install` both mutate class-level state that would otherwise
pace, log and re-classify every later test. That fixture imports the Facebook extractor eagerly
rather than probing `sys.modules`: a guard that only checks what happened to be imported is the kind
of check that reports success because it checked nothing.

On `_no_real_spawn` specifically: Before anonymous mode there was an accidental guard — a cookie-less job failed before
reaching `spawn_worker` — so tests could create jobs without patching anything. That is gone by
design, so `tests/conftest.py` patches a harmless default; `fake_spawn`/`capture_spawn` still
override it.

**The downloads dir is checked before spawning, including the per-platform subdirectory.** A
writable root with an unwritable child is the nasty case: reads succeed, so archived files report
`skipped` and the job looks healthy while every actual download fails. Typically caused by seeding
the tree as root onto an NFS export with `root_squash` (lands as `nobody:nogroup` 0755).

**Profile metadata lives outside `downloads/`** (`<data_dir>/profiles/…/metadata.json`) so it never
appears in `GET /api/files` or inside a profile zip, and gallery-dl can't clobber it. The on-disk
files are the source of truth; `metadata.json` is a rebuildable index.

**Reconcile keys off `media_paths()` (any `file` event), the zip route off `downloaded_paths()`
(downloaded only).** A stopped job — or any re-run that found everything already archived — emits
skip events only; keying reconcile off downloads alone left its gallery showing stale counts. The
zip must keep shipping only newly fetched files.

**The per-profile archive key ≠ the profile folder name.** The archive is named from the URL
(`profiles/urls.py:extract_username`), the folder from gallery-dl's `{username}` (a display name).
That is why `archive_path` is stored in `metadata.json` — deletion needs it.

**Every filesystem path from a request goes through `api/paths.py:resolve_within`.** It is the single
traversal guard for `/api/files`, profile files, thumbnails, and zips.

**No handler may touch the downloads tree synchronously — the media dir is usually a NAS.** A
blocking walk inside an `async def` is not a slow endpoint, it is a **global outage**: the event
loop serves nothing while it runs, `/health` included, so Docker marks the backend unhealthy and
every other request — SSE streams, `/api/jobs` — stalls with it. Observed live 2026-09-05 on an
install with **427,009 files over NFS**; `GET /api/files` was doing exactly this and the main thread
sat in uninterruptible disk sleep while the 5 s health probe timed out three times.

`files/index.py` is the pattern, and all three parts are load-bearing:
`asyncio.to_thread` (as `ProfileStore.reconcile` already did), a **single-flight lock**, and a short
TTL. The lock is not an optimisation — moving the walk off the loop *removes* the accidental
serialisation the block used to provide, so without it five page loads become five concurrent
427k-file walks, strictly worse than the bug. `routes_profiles.py`'s zip build threads both halves
for the same reason: it walks the profile *and* deflates every byte.

`/api/files` is also **paged** (`limit`, default 1000, plus a real `total`) — serialising the full
tree was tens of megabytes of JSON for a table that shows a screenful.

⚠️ **Testing this property is where it gets subtle, and two obvious tests both passed with the
offload deleted.** "Start the listing, sleep, then time `/health`" fails because the blocking walk
completes *inside that very sleep*, leaving the timer to measure an idle loop. "Launch both, assert
`/health` finishes first" fails because ordering through `httpx` is decided by its own await points,
not by the blocking. `tests/files/test_index.py` instead **counts how many times the loop got to run
while a walk was in flight** — zero means blocked, dozens means threaded — which is the property
itself rather than a proxy for it. Both it and the single-flight test were verified by sabotage.

**Pacing is adaptive, and the shape of the problem is asymmetric.** Instagram gets ~30 posts per
JSON request, so a delay amortizes away; Facebook fetches one full 1-3 MB HTML page *per photo*
(`facebook.py:extract_set` walks a singly-linked list of ids — not parallelizable, no cursor), so
it pays the delay once per image. A fixed delay large enough to be safe on a long Facebook run
therefore makes every short one ~5x slower than it needs to be.

`gallerydl/pacing.py` owns this. Three things worth knowing before touching it:

- **It hands gallery-dl a delay; it never sleeps itself.** `Extractor.request` computes
  `interval - (now - request_timestamp)`, and image downloads bypass it entirely, so the delay is a
  floor on request *spacing*, not an addend — at a 1 s floor against a 1-2 s page fetch the added
  sleep is usually zero. `next_delay` must stay a **pure read**: the retry loop calls it again per
  retry to floor its own backoff (`seconds = max(retry, request, 429)`), so a state-advancing
  getter would corrupt the model.
- **`install()` patches classes, not an instance**, for the same reason the progress hooks are a
  postprocessor: profile extraction spawns child jobs with their own extractors and they share one
  request budget. It wraps `Extractor.request` (inject the delay, classify raised errors), wraps
  `Extractor._init_session` to attach a **`requests` response hook** — that hook, not the return
  value, is where responses are judged, because only it sees intermediate retries, redirect hops
  and image downloads (a CDN 429 is invisible otherwise) — and adds a root `logging.Handler`.
- **That log handler is not optional: on Facebook a rate limit is an HTTP 200.** A soft block is a
  photo page whose image URL will not parse, and the only in-band evidence is gallery-dl's
  `"Failed to find photo download URL"` warning. It reuses `errors.py:detect_rate_limit`, so the
  live sensor and the reported failure reason cannot drift apart.

**A media download is judged but never counted as clean, and the asymmetry is load-bearing.**
Downloads share `extractor.session` (`downloader/common.py`), so they reach the response hook — but
they bypass `Extractor.request`, so `begin_request()` never counts them toward the ramp and the
pacer never spaces them. On Instagram they outnumber extractor requests ~30:1 (~30 images per JSON
page). While they advanced the clean streak, `decay_after=10` + `growth=3.0` meant ~20 of them
returned a ceiling-level penalty to the floor — *inside a single page* — so the controller could not
hold a back-off no matter what it detected, and `adaptive` behaved as `fixed(floor)` in exactly the
regime it exists for. `observe` therefore judges their **status** (a CDN 429 on an image is real)
but withholds `clean()`. Don't "simplify" that branch away.

**The body branch of `_classify` has never fired, and `_buffered_body` explains why.** `requests`
dispatches response hooks at `sessions.py:791` and buffers the body at `sessions.py:827`, so
`_content_consumed` is always `False` when the hook runs and `_buffered_body()` returns `b""`.
Facebook is covered anyway by the log sensor; Instagram had no equivalent, which is how its
HTTP-200 throttles went unseen. `telemetry.capture_body` is the safe way to read one: for a
**non-streamed** response `.content` is free (requests performs the identical read moments later and
memoises it), and the `stream` kwarg is the only reliable discriminator — `response.raw.closed` is
`False` for streamed and non-streamed alike at hook time. `_NULL_RESPONSE_STATUS` (900) is likewise
inert via `observe`: gallery-dl builds `NullResponse` itself and it never traverses `Session.send`.

**The volume ramp, not the reactive back-off, is what protects a long run.** A hard block is
terminal by design — `facebook.py:photo_page_request_wrapper` raises `AbortExtraction` the moment
it sees the block page — so backing off afterwards achieves nothing. Instead the *floor* rises with
cumulative requests (`1 s -> ~4.8 s by request 767`, the point at which a real account was blocked,
8 s ceiling at ~1400). Short profiles stay fast; long ones end up more cautious than the old fixed
3-8 s.

**`MIN`/`MAX` change meaning with the mode**: `adaptive` = floor + back-off ceiling, `fixed` = a
random range per request (the old behaviour). Resolution is per-job `options.pacing` -> the runtime
store (`<data_dir>/pacing.json`, editable in Settings with no restart) -> env `Settings` ->
`_PLATFORM_DEFAULTS`. Like `anonymous`, `pacing` is a **top-level payload key** popped out of
`options` by `_run_job`, because `config_builder` only reads keys it knows about.

**`per_file` is a second, independent axis, and it deliberately does NOT go through the pacer.**
`MIN`/`MAX` space the extractor's API requests; `per_file` spaces the **image downloads** one
request releases, which on Instagram outnumber API requests ~30:1 and were previously unpaced
entirely — measured runs showed a 0.8-2.7 s median gap between consecutive files while the API
calls sat 20 s apart, and it is that burst, not the run average, that a rate limiter reacts to.
It reaches gallery-dl as its own `sleep` option from `config_builder._resolve_sleep_file`, emitted
as a ±15% band rather than a constant. Three consequences worth knowing:

- **It works in `fixed` mode**, where `worker.py`'s `mode != "adaptive"` gate installs no pacer at
  all. A pacer-based implementation would have been silently dead for anyone on `fixed`.
- **Skips never pay it.** `DownloadJob.handle_url` returns on both the archive check and the
  on-disk check *before* `extractor.sleep(self.sleep(), "download")`, so re-running a downloaded
  profile is unaffected. `tests/gallerydl/test_upstream_pins.py` pins that ordering, because a
  gallery-dl bump that moved the sleep would turn a seconds-long refresh into an hours-long one
  without raising anything.
- **It is additive, unlike everything else here.** `Extractor.sleep` is a bare `time.sleep`, where
  `_interval_request` subtracts elapsed time. So the observed gap is the value plus the download,
  which is why the band is centred on the operator's number rather than starting at it.

**`normalize_pacing` returns a freshly built dict, so a field it does not name is dropped** at all
four call sites. `per_file` is therefore `float | None` there: **absent means "no opinion", not
zero**, and `merge_pacing` fills it from the layer below. Without that a `pacing.json` written
before the field existed — or any per-job override, since `UrlForm` builds its block field by
field — would read as "the operator turned the per-image delay off" and veto the default. An
explicit `0` is still an opinion and is kept.

**The pacing ceiling is coupled to the stall detector** — `_resolve_pacing` clamps it to
`stall_cap_seconds / 4`, and `pacing.HARD_MAX_DELAY` clamps a hand-written payload — because a job
legitimately backed off past the progress deadline gets killed as stalled, and a Facebook retry
re-walks the linked list from the top. The binding case is not steady state but the fallback stall
below.

**A `pacing` event must not reset the progress clock** (sleeping is the opposite of progress), and
it is emitted **on change only**, >=0.25 s apart, at most every 5 s, capped at 200 per job:
`JobState.events` is a `deque(maxlen=5000)` that `media_paths()` and the zip route read `file`
events back out of, so a chatty event type silently truncates a job's downloads. Comparing against
the last *reported* delay rather than the last one is deliberate — the ramp moves in millisecond
steps and would otherwise climb from 1 s to 8 s in total silence.

**Two Facebook defaults exist purely to bound wall-clock, and both look like gallery-dl tuning
knobs rather than what they are.** `include` is `photos` alone (not `photos,albums`): an album
re-walks photos the main set already covered, and `DownloadJob.handle_url` checks the archive only
*after* fetching the page, so the duplicate walk costs full page fetches for no new files.
And `fallback-retries` is `1` with `sleep-429` **shaped** as `"exponential:2:0:60=15"` — at
gallery-dl's defaults an unparseable photo costs `2 x (60 + 1) s` of dead sleep emitting no
`prepare` or `file`, so two consecutive ones exceed `stall_floor_seconds` on their own and get a
healthy job killed. Never *flatten* `sleep-429`: `downloader/http.py` inherits
`extractor._interval_429` for CDN 429s, so a small constant is how a soft block becomes a hard one.

**Adding a `Settings` field means touching five places**: `config.py`, `.env.example`,
`backend/.env.example` (used when the backend runs standalone), and the `environment:` block of
*both* compose files (env vars are uppercase field names).

**`/health` is at the backend root, not under `/api`.** The frontend serves its own `/api/health`
locally (Dockerfile HEALTHCHECK); the catch-all proxy only forwards `/api/*`. That is also why the
frontend container reports **healthy while the backend is dead** — its probe never crosses the wire.

**The proxy must catch its own upstream fetch, and the reason is `asJson`.** An uncaught
`ECONNREFUSED` escapes the route handler and Next answers with a *plain-text* 500 whose body is the
literal string `Internal Server Error`. `lib/api.ts:asJson` falls back to `res.statusText` when the
body will not parse as JSON, so that string lands verbatim in all six `alert alert-error` panels and
tells the operator nothing. Returning `Response.json({ detail }, { status: 502 })` is the whole fix:
`asJson` already *prefers* a JSON `detail`, so every existing error surface improves with no
component change. Don't "simplify" the try/catch away — the failure it prevents is invisible in
tests and only shows up when the backend is actually down.

**The "system" theme is the *absence* of `data-theme` on `<html>`, not a value.** DaisyUI emits
`--prefersdark` as `@media (prefers-color-scheme: dark) { :root:not([data-theme]) { … } }`, so
leaving the attribute off is what hands the palette and `color-scheme` to the OS — live, with no
`matchMedia` listener. Setting it makes the `:not()` guard stand down, which is what pins an
explicit choice. That is why `layout.tsx` renders **no** `data-theme` and only an inline `<head>`
script adds one, and why neither theme control holds React state: all three modes are
distinguishable in CSS (`globals.css`), so the sun/moon and the ✓ are right before hydration. The
navbar menu (`ThemeToggle.tsx`) and the Settings card (`ThemeSetting.tsx`) both key off
`THEME_OPTIONS`' `marker` classes in `lib/theme.ts`, which is what keeps them in step with no shared
state; the navbar owns the one cross-tab `storage` listener because it is mounted on every page.

Two corollaries, both about **unlayered CSS outranking everything in Tailwind's `@layer
utilities`**: never put an unlayered `body { background: … }` in `globals.css` — one silently pins
the page to a single color and turns the `bg-base-200` on `<body>` into dead code, which is exactly
what kept the app light-only. And keep the selected-option emphasis scoped to `.theme-label` rather
than the whole row, or it will outrank the `text-xs`/`font-*` utilities on the Settings row's
description.

**The extension popup repeats the same mechanism, with two constraints that invert the usual
choices** (`extension/theme.js` + the `<style>` in `popup.html`). MV3's extension CSP is
`script-src 'self'`, so the pre-paint script must be an **external file** — an inline `<script>`
silently never runs. And the preference lives in **`localStorage`, not `chrome.storage.local`**
like `serverUrl` does: chrome.storage is async, and awaiting it means the whole 320 px popup paints
light and then flips. Don't "fix" either one. The selected-mode selectors are all prefixed `:root`
because `:hover` counts as a class-level component — `.theme button:hover` is (0,2,1) and would
outrank a bare `[data-theme="dark"] .theme-mode-dark` at (0,2,0).

## Testing conventions
`asyncio_mode = "auto"` (no `@pytest.mark.asyncio`). Tests build a fresh app per test via the
`app`/`tmp_settings` fixtures so `app.state` singletons are isolated. Manager tests monkeypatch
`gallery_dl_web.jobs.manager.spawn_worker` with the `fake_spawn` fixture (`FakeProc` yields canned
JSON lines with an optional per-line delay — that delay is how stall behavior is tested). Worker
tests monkeypatch `gallery_dl.job.DownloadJob`, so no network is needed anywhere in the suite.

**A fixture copied from a live run must be sanitized before it is committed.** Replace every real
username, profile id and media id with a placeholder — the repo is public, and those identify a
third party, not you. `tests/gallerydl/test_errors.py` is the cautionary example: it was committed
as "verbatim" stderr and carried a real Facebook username plus that person's photo and album ids
through several releases before a history rewrite removed them. Keep the *shape* of the real
output — that is what makes the fixture worth having — and nothing else.

## Frontend note
`frontend/AGENTS.md` (aliased by `frontend/CLAUDE.md`) applies: this Next.js version has breaking
changes vs. training data — read the relevant guide in `node_modules/next/dist/docs/` before writing
frontend code.

## Conventions
- Conventional commits. `uv.lock` and `package-lock.json` are tracked.
- Never commit `backend/data/`, `*.sqlite`, `.env`, or cookies.
- Keep `ROADMAP.md` current when a phase status changes (living document).

## Docs to keep in sync
`README.md` (quickstart + host-dir/NAS setup + rate limits), `ROADMAP.md` (phase status),
`docs/event-contract.md` + `frontend/src/lib/events.ts` (the SSE schema — always both), and
`.env.example` + `backend/.env.example` + both compose files (any new `Settings` field).
