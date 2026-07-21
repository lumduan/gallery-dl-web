# gallery-dl-web (backend)

FastAPI service that wraps the [`gallery-dl`](https://github.com/mikf/gallery-dl) engine to
download Instagram and Facebook images, exposing a JSON API + SSE progress stream consumed by the
Next.js frontend.

See the monorepo root [`README.md`](../README.md) for the full overview, architecture, and setup.

## Run (dev)

```bash
uv sync --all-groups
uv run python -m gallery_dl_web          # serves on :8000
```

## Run (tests)

```bash
uv run pytest                            # with coverage gate (>=80%)
uv run ruff check . && uv run mypy src tests
```
