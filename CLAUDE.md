# CLAUDE.md — gallery-dl-web

Two-service web app wrapping `gallery-dl` to download Instagram & Facebook images.

## Stack
- **Backend** (`backend/`, Python 3.12): FastAPI + `gallery-dl` + sse-starlette. uv, src-layout,
  hatchling, ruff (E/F/I/UP/B/SIM, line 100), mypy strict on `src`, pytest ≥80% coverage.
- **Frontend** (`frontend/`, Node 24): Next.js 16 + React 19 + Tailwind v4 + DaisyUI 5.
- **Containers**: multi-stage Dockerfiles, non-root UID 1001; `docker-compose.yml` (prod) +
  `docker-compose.dev.yml` (dev overlay). Images → `ghcr.io/lumduan/gallery-dl-web-{backend,frontend}`.

## Architecture in one paragraph
`POST /api/jobs` → `JobManager` spawns a subprocess `python -m gallery_dl_web.gallerydl.worker`, sends
its config (incl. cookies) over **STDIN**, and streams the worker's JSON-lines stdout to SSE
subscribers. One process per job isolates gallery-dl's global `config` state. Cookies never touch
argv, disk, logs, or API responses (repo is public). The Next.js frontend proxies `/api/*` to the
backend via a catch-all route (`src/app/api/[...path]/route.ts`) that reads `BACKEND_URL` at request
time — NOT `next.config` rewrites, which bake the destination in at build time.

## Commands
Backend (`cd backend`): `uv sync --all-groups` · `uv run python -m gallery_dl_web` ·
`uv run pytest` · `uv run ruff check .` · `uv run mypy src`.
Frontend (`cd frontend`): `npm install` · `npm run dev` · `npm run build` ·
`npm run typecheck` · `npm run lint`.
Compose: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up` (dev) · `docker compose up -d` (prod).

## Key files
- `backend/src/gallery_dl_web/gallerydl/worker.py` — subprocess entry; the load-bearing contract.
- `backend/src/gallery_dl_web/jobs/manager.py` — asyncio orchestrator (spawn/fan-out/replay/GC).
- `backend/src/gallery_dl_web/gallerydl/config_builder.py` — pure payload→`config.set` translator.
- `backend/src/gallery_dl_web/api/routes_jobs.py` — SSE endpoint + zip.
- `frontend/src/app/jobs/[id]/page.tsx` + `components/JobProgress.tsx` — SSE consumer.
- `docs/event-contract.md` — the shared SSE schema (change both sides together).

## Conventions
- Conventional commits. `uv.lock` and `package-lock.json` are tracked.
- Never commit `backend/data/`, `*.sqlite`, `.env`, or cookies.
- Keep `ROADMAP.md` current when a phase status changes (living document).
