# syntax=docker/dockerfile:1
# gallery-dl-web backend — FastAPI driving gallery-dl.
# Multi-stage, non-root UID 1001 (house convention shared with m3u8-downloader / edubuddy).

# === Builder ===
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Install dependencies first (cached layer). README lives one level up in the monorepo; create a
# stub so uv sync doesn't fail on the readme reference.
COPY pyproject.toml uv.lock ./
RUN printf '# gallery-dl-web backend\n' > README.md && uv sync --frozen --no-dev --no-install-project

# Install the project itself.
COPY src ./src
RUN uv sync --frozen --no-dev

# === Runtime ===
FROM python:3.12-slim AS runtime

ENV PYTHONPATH=/app \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    DOWNLOADS_DIR=/data/downloads \
    COOKIES_PATH=/data/cookies.json

RUN groupadd --gid 1001 app && useradd --uid 1001 --gid app --create-home --shell /bin/bash app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app src ./src

RUN mkdir -p /data/downloads /data/archive && chown -R app:app /data

USER app

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["python", "-m", "gallery_dl_web"]
