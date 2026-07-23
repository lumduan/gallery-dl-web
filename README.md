# gallery-dl-web

A small web app to download **Instagram** and **Facebook** images by wrapping the
[`gallery-dl`](https://github.com/mikf/gallery-dl) engine. Paste a URL, watch live progress, and
download the results — packaged as two containers (FastAPI + Next.js).

```
Browser ──► Next.js :3000 ──(proxy /api/*)──► FastAPI :8000
   ▲ EventSource (SSE)                            │
   └──────────────────────────────────────────────┤
                                                  ▼
                                   JobManager (asyncio, in-memory)
                                                  │ spawn subprocess, send config over STDIN
                                                  ▼
                          `python -m gallery_dl_web.gallerydl.worker`
                            ├─ gallery_dl.config.set(...)   (cookies, dir, templates, pacing)
                            ├─ DownloadJob(url).run() + hooks (file/skip/error)
                            ├─ emits one JSON-line event per file → SSE fan-out
                            └─ heartbeats while gallery-dl is quiet (it enumerates for minutes)
                                                  │
                          media → DOWNLOADS_DIR (default /data/downloads, a named volume)
                          gallery-dl SQLite archive → re-run de-dup / resume
```

## Why a subprocess per job?

`gallery_dl.job.DownloadJob` mutates module-global `config`/extractor state, so running it in-process
inside the server would let concurrent jobs corrupt each other's cookies. Each job runs in its own
Python process for complete isolation. Cookies are passed over **STDIN** (never argv / disk / logs) —
important because this repo is public.

## Quickstart

### 1. Configure cookies (required)

gallery-dl disables password login for Instagram, so auth is cookie-based. You provide your own
logged-in cookies — there are two ways:

**Option A — browser extension (recommended).** Load the [`extension/`](extension/README.md) folder
unpacked in Chrome/Edge/Brave, set your gallery-dl-web server URL in its popup, then click
**Send Instagram session** / **Send Facebook cookies** while logged into instagram.com / facebook.com.
It reads **your own** cookies and sends them to the app — one click, refreshable when IG/FB rotates
the session. See [`extension/README.md`](extension/README.md).

**Option B — manual paste (fallback).** Open the app's **Settings** page:

- **Instagram** — copy the `sessionid` cookie: log into instagram.com → DevTools → **Application**
  → **Cookies** → copy `sessionid` (HttpOnly, so via DevTools, not JS).
- **Facebook** — export a Netscape `cookies.txt` (e.g. with "Get cookies.txt LOCALLY") while logged
  into facebook.com. ⚠️ FB cookies (`c_user` + `xs`) grant **full account access** — use a dedicated
  account.

Either way, values are stored only in the backend's `/data/cookies.json` (mode 0600, gitignored) and
are never returned over the API.

### 2. Run it

```bash
cp .env.example .env

# Prod
docker compose up -d

# Dev (hot reload on both services) — the dev file is STANDALONE, not an overlay on the prod one
docker compose -f docker-compose.dev.yml up
```

> Don't merge the two compose files. `docker-compose.dev.yml` replaces the prod definition rather
> than extending it; combining them pairs the prod `build: ./frontend` with the dev
> `image: node:24-alpine`, so Docker builds the production frontend and tags it as your local Node
> base image.

Open <http://localhost:3000>, paste a public IG/FB URL, and watch it download.

### Storing media on another disk / a NAS

By default everything lives in the `gallery-data` named volume. To put media somewhere else, set
`DOWNLOADS_DIR` **and** enable the bind-mount overlay — the variable alone does nothing, because
the path also has to exist inside the container:

```bash
# .env
DOWNLOADS_DIR=/mnt/downloads/gallery
COMPOSE_FILE=docker-compose.yml:docker-compose.hostdir.yml
```

Compose reads `COMPOSE_FILE` automatically, so `docker compose up -d` still works unchanged. The
directory (and everything under it) must be writable by **UID 1001**, the non-root app user. If you
seed it with existing media, copy it as UID 1001 — copying as root onto an NFS export with
`root_squash` lands the tree as `nobody:nogroup` mode 0755, which reads fine but silently fails
every download. The backend refuses to start a job when it detects this
(`reason: downloads-dir-unwritable`).

## Project layout

```
gallery-dl-web/
├── backend/          # FastAPI + gallery-dl (uv, src-layout, ruff, mypy-strict, pytest ≥80%)
│   └── src/gallery_dl_web/{api,jobs,gallerydl,cookies,profiles,schemas}/
├── frontend/         # Next.js 16 + React 19 + Tailwind v4 + DaisyUI 5
│   └── src/{app,components,lib}/
├── extension/        # Manifest V3 browser extension — sends your IG/FB cookies to Settings
├── docs/event-contract.md   # the SSE JSON schema (pinned, shared by both services)
├── docker-compose.yml       # prod
├── docker-compose.dev.yml   # dev (standalone — do NOT merge with the prod file)
└── docker-compose.hostdir.yml  # optional: bind-mount a host/NAS dir for media
```

## Development

**Backend** (`backend/`):

```bash
uv sync --all-groups
uv run python -m gallery_dl_web     # serves API on :8000
uv run pytest                       # tests + ≥80% coverage gate
uv run pytest tests/jobs -q --no-cov   # a subset — --no-cov skips the coverage gate
uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

**Frontend** (`frontend/`):

```bash
npm install
npm run dev          # :3000 (set BACKEND_URL=http://localhost:8000)
npm run build        # standalone production build
npm run typecheck && npm run lint
```

## API

| Method | Path                         | Purpose                                  |
| ------ | ---------------------------- | ---------------------------------------- |
| POST   | `/api/jobs`                  | Create a download job → `202 {job_id}`   |
| GET    | `/api/jobs`                  | List jobs                                |
| GET    | `/api/jobs/{id}`             | Job snapshot                             |
| GET    | `/api/jobs/{id}/events`      | **SSE** progress stream                  |
| GET    | `/api/jobs/{id}/zip`         | Download a job's files as a zip          |
| GET    | `/api/settings`              | `{has_ig, has_fb}` (booleans only)       |
| PUT    | `/api/settings/cookies`      | Set IG `sessionid` / FB cookies.txt      |
| GET    | `/api/files`                 | List downloaded files                    |
| GET    | `/api/files/download?path=`  | Download one file (path-traversal-safe)  |
| GET    | `/api/profiles`              | List profiles (avatar, image/video counts) |
| GET    | `/api/profiles/{p}/{name}`   | Profile metadata + per-file thumb/file URLs |
| GET    | `/api/profiles/{p}/{name}/thumb/{file}` | Generated thumbnail (Pillow, cached) |
| GET    | `/api/profiles/{p}/{name}/file/{file}`  | Full-size file                        |
| GET    | `/api/profiles/{p}/{name}/zip` | Profile .zip (auto-deleted after `ZIP_TTL_SECONDS`) |
| DELETE | `/api/profiles/{p}/{name}`   | Delete profile (files + archive + metadata) |
| GET    | `/health`                    | `{"status":"ok"}`                        |

## Profile management

Downloads are organized per profile under `<DOWNLOADS_DIR>/<platform>/<username>/` (gallery-dl's
`{username}` template). Each profile is indexed in a `metadata.json` (under `<DATA_DIR>/profiles/`,
not in `downloads/`) with image/video counts and file list, rebuilt automatically after each job.

- **Incremental**: re-downloading a profile only fetches new media (per-profile gallery-dl archive).
- **Gallery UI**: the **Profiles** page shows a card per profile (avatar + name + counts); click a
  card for a thumbnail grid (Pillow thumbnails, generated on demand). Click a thumbnail for the full
  image; **Download .zip** builds `<name>.zip` (auto-deleted `ZIP_TTL_SECONDS` after last access);
  **Delete profile** removes files + per-profile archive + metadata to free storage.
- **Config**: `DOWNLOADS_DIR` (where media lives — see
  [Storing media on another disk / a NAS](#storing-media-on-another-disk--a-nas)),
  `ZIP_TTL_SECONDS` (default 300), `THUMBNAIL_SIZE` (default 300) — see `.env.example`.

## Rate limits and long-running jobs

Both platforms throttle scraping, so requests are paced via gallery-dl's `sleep-request`:
Instagram 6–12 s, Facebook 3–8 s (`INSTAGRAM_`/`FACEBOOK_SLEEP_REQUEST_MIN`/`MAX`). Facebook is the
harsher one — with no delay it blocked an account after ~767 images in a single run. **If you get
blocked, raise those values and wait before retrying**; a block costs far more time than the delay.
Set `MAX=0` to disable pacing.

A job is guarded by two independent deadlines, because pacing makes silence normal:

- **liveness** — the worker emits a `heartbeat` every `HEARTBEAT_SECONDS`; if even those stop for
  `STALL_LIVENESS_SECONDS` the process is wedged and is killed.
- **progress** — no `prepare`/`file` event. Before the first one that's the *warm-up* budget
  (`STALL_WARMUP_SECONDS`, default 600 s), since gallery-dl is silent for minutes while walking a
  profile; afterwards it adapts to the observed inter-file rate.

Re-running a profile is cheap: the per-profile gallery-dl archive means already-fetched files come
back as `skipped`, so an interrupted or blocked run resumes where it stopped.

## Security

This repo is **public**, so credentials are handled carefully:
- Cookies live only in `backend/data/cookies.json` (0600, gitignored, in a named volume).
- Cookies are passed to the worker over **STDIN**, never argv / env / logs / API responses.
- `GET /api/settings` returns only boolean presence flags — never the values.
- `/api/files` rejects path traversal (`..`); archive SQLite files are hidden.
- Both images run as non-root UID 1001.

See [the roadmap](ROADMAP.md) for status and [the event contract](docs/event-contract.md) for the
SSE schema. MIT licensed.
