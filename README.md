# gallery-dl-web

A small web app to download **Instagram** and **Facebook** images by wrapping the
[`gallery-dl`](https://github.com/mikf/gallery-dl) engine. Paste a URL, watch live progress, and
download the results — packaged as two containers (FastAPI + Next.js).

```
Browser ──► Next.js :3000 ──(rewrite /api/*)──► FastAPI :8000
   ▲ EventSource (SSE)                            │
   └──────────────────────────────────────────────┤
                                                  ▼
                                   JobManager (asyncio, in-memory)
                                                  │ spawn subprocess, send config over STDIN
                                                  ▼
                          `python -m gallery_dl_web.gallerydl.worker`
                            ├─ gallery_dl.config.set(...)   (cookies, dir, templates)
                            ├─ DownloadJob(url).run() + hooks (file/skip/error)
                            └─ emits one JSON-line event per file → SSE fan-out
                                                  │
                          files land in /data/downloads (named volume)
                          gallery-dl SQLite archive → re-run de-dup
```

## Why a subprocess per job?

`gallery_dl.job.DownloadJob` mutates module-global `config`/extractor state, so running it in-process
inside the server would let concurrent jobs corrupt each other's cookies. Each job runs in its own
Python process for complete isolation. Cookies are passed over **STDIN** (never argv / disk / logs) —
important because this repo is public.

## Quickstart

### 1. Configure cookies (required)

gallery-dl disables password login for Instagram, so auth is cookie-based.

**Instagram** — copy the `sessionid` cookie:
- Log into instagram.com in a desktop browser → DevTools → **Application** → **Cookies** →
  `https://www.instagram.com` → copy the value of `sessionid` (HttpOnly, so via DevTools, not JS).

**Facebook** — export a Netscape `cookies.txt`:
- Use a browser extension (e.g. "Get cookies.txt LOCALLY") while logged into facebook.com.
- ⚠️ Facebook cookies (`c_user` + `xs`) grant **full account access** — use a dedicated account.

Enter both in the **Settings** page of the running app. Values are stored only in the backend's
`/data/cookies.json` (mode 0600, gitignored) and are never returned over the API.

### 2. Run it

```bash
# Dev (hot reload on both services; needs Docker)
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.dev.yml up

# Prod (prebuilt ghcr images)
docker compose up -d
```

Open <http://localhost:3000>, paste a public IG/FB URL, and watch it download.

## Project layout

```
gallery-dl-web/
├── backend/          # FastAPI + gallery-dl (uv, src-layout, ruff, mypy-strict, pytest ≥80%)
│   └── src/gallery_dl_web/{api,jobs,gallerydl,cookies,schemas}/
├── frontend/         # Next.js 16 + React 19 + Tailwind v4 + DaisyUI 5
│   └── src/{app,components,lib}/
├── docs/event-contract.md   # the SSE JSON schema (pinned, shared by both services)
├── docker-compose.yml       # prod
└── docker-compose.dev.yml   # dev overlay
```

## Development

**Backend** (`backend/`):

```bash
uv sync --all-groups
uv run python -m gallery_dl_web     # serves API on :8000
uv run pytest                       # tests + ≥80% coverage gate
uv run ruff check . && uv run mypy src
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
| GET    | `/health`                    | `{"status":"ok"}`                        |

## Security

This repo is **public**, so credentials are handled carefully:
- Cookies live only in `backend/data/cookies.json` (0600, gitignored, in a named volume).
- Cookies are passed to the worker over **STDIN**, never argv / env / logs / API responses.
- `GET /api/settings` returns only boolean presence flags — never the values.
- `/api/files` rejects path traversal (`..`); archive SQLite files are hidden.
- Both images run as non-root UID 1001.

See [the roadmap](ROADMAP.md) for status and [the event contract](docs/event-contract.md) for the
SSE schema. MIT licensed.
