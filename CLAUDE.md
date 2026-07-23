# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Two-service web app wrapping `gallery-dl` to download Instagram & Facebook images.

## Stack
- **Backend** (`backend/`, Python 3.12): FastAPI + `gallery-dl` + sse-starlette + Pillow. uv,
  src-layout, hatchling, ruff (E/F/I/UP/B/SIM, line 100), mypy strict on `src`, pytest ≥80% coverage.
- **Frontend** (`frontend/`, Node 24): Next.js 16 + React 19 + Tailwind v4 + DaisyUI 5.
- **Containers**: multi-stage Dockerfiles, non-root UID 1001; `docker-compose.yml` (prod) +
  `docker-compose.dev.yml` (dev, **standalone**). Images → `ghcr.io/lumduan/gallery-dl-web-{backend,frontend}`.

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
`node:24-alpine`. (`README.md` still documents the merged form — see "Known doc drift".)

`docker-compose.hostdir.yml` is a separate, opt-in overlay that bind-mounts a host/NAS directory for
media. Setting `DOWNLOADS_DIR` alone does **nothing** — the path also has to exist inside the
container. Enable both together in `.env`:
`DOWNLOADS_DIR=/mnt/...` plus `COMPOSE_FILE=docker-compose.yml:docker-compose.hostdir.yml`
(compose reads `COMPOSE_FILE` automatically, so `docker compose up -d` is unchanged).

## Key files
- `backend/src/gallery_dl_web/gallerydl/worker.py` — subprocess entry; the load-bearing contract.
- `backend/src/gallery_dl_web/jobs/manager.py` — asyncio orchestrator (spawn/fan-out/replay/stall-retry/GC).
- `backend/src/gallery_dl_web/gallerydl/config_builder.py` — pure payload→`config.set` translator.
- `backend/src/gallery_dl_web/api/routes_jobs.py` — SSE endpoint + zip.
- `backend/src/gallery_dl_web/profiles/store.py` — per-profile `metadata.json` reconciliation.
- `frontend/src/app/jobs/[id]/page.tsx` + `components/JobProgress.tsx` — SSE consumer.
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

**Exactly one terminal event per job** (`completed` | `failed`), always. The manager synthesizes one
if the worker dies silently, is cancelled, or exhausts retries; `stalled`/`retrying` are non-terminal.

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

**The downloads dir is checked before spawning, including the per-platform subdirectory.** A
writable root with an unwritable child is the nasty case: reads succeed, so archived files report
`skipped` and the job looks healthy while every actual download fails. Typically caused by seeding
the tree as root onto an NFS export with `root_squash` (lands as `nobody:nogroup` 0755).

**Profile metadata lives outside `downloads/`** (`<data_dir>/profiles/…/metadata.json`) so it never
appears in `GET /api/files` or inside a profile zip, and gallery-dl can't clobber it. The on-disk
files are the source of truth; `metadata.json` is a rebuildable index.

**The per-profile archive key ≠ the profile folder name.** The archive is named from the URL
(`profiles/urls.py:extract_username`), the folder from gallery-dl's `{username}` (a display name).
That is why `archive_path` is stored in `metadata.json` — deletion needs it.

**Every filesystem path from a request goes through `api/paths.py:resolve_within`.** It is the single
traversal guard for `/api/files`, profile files, thumbnails, and zips.

**Both platforms are rate-limited and both are paced** via gallery-dl's `sleep-request`
(`<PLATFORM>_SLEEP_REQUEST_MIN/MAX`, injected into the job payload by `_build_payload`, overridable
per-job through the API's `options`). Facebook blocked an account after ~767 images fetched with no
delay. Raise the values after a block; a block costs far more time than the delay. `MAX=0` disables.

**Adding a `Settings` field means touching four places**: `config.py`, `.env.example`, and the
`environment:` block of *both* compose files (env vars are uppercase field names).

**`/health` is at the backend root, not under `/api`.** The frontend serves its own `/api/health`
locally (Dockerfile HEALTHCHECK); the catch-all proxy only forwards `/api/*`.

## Testing conventions
`asyncio_mode = "auto"` (no `@pytest.mark.asyncio`). Tests build a fresh app per test via the
`app`/`tmp_settings` fixtures so `app.state` singletons are isolated. Manager tests monkeypatch
`gallery_dl_web.jobs.manager.spawn_worker` with the `fake_spawn` fixture (`FakeProc` yields canned
JSON lines with an optional per-line delay — that delay is how stall behavior is tested). Worker
tests monkeypatch `gallery_dl.job.DownloadJob`, so no network is needed anywhere in the suite.

## Frontend note
`frontend/AGENTS.md` (aliased by `frontend/CLAUDE.md`) applies: this Next.js version has breaking
changes vs. training data — read the relevant guide in `node_modules/next/dist/docs/` before writing
frontend code.

## Conventions
- Conventional commits. `uv.lock` and `package-lock.json` are tracked.
- Never commit `backend/data/`, `*.sqlite`, `.env`, or cookies.
- Keep `ROADMAP.md` current when a phase status changes (living document).

## Known doc drift (verify before trusting)
- `README.md` dev command and `ROADMAP.md` phase 3 still describe `next.config` rewrites / the merged
  compose invocation; the code uses the catch-all proxy and the standalone dev file.
- `frontend/src/lib/api.ts` header comment also still says "Next.js rewrites".
