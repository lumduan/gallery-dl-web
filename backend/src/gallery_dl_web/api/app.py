"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from gallery_dl_web.api import (
    routes_files,
    routes_health,
    routes_jobs,
    routes_profiles,
    routes_settings,
)
from gallery_dl_web.config import Settings, get_settings
from gallery_dl_web.cookies.store import CookieStore
from gallery_dl_web.jobs.manager import JobManager
from gallery_dl_web.profiles.store import ProfileStore

logger = logging.getLogger(__name__)


class _SuppressHealthAccessLogs(logging.Filter):
    """Drop successful /health access-log lines.

    The container healthcheck polls every 15s. At INFO that buries every real warning — during one
    incident the single 'could not create path' line was lost among hundreds of these.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.args and "/health" in str(record.args))


async def _gc_loop(manager: JobManager) -> None:
    while True:
        await asyncio.sleep(300)
        try:
            removed = await manager.gc()
            if removed:
                logger.info("gc removed %d terminal job(s)", removed)
        except Exception:  # noqa: BLE001
            logger.exception("job gc failed")


def _reap_zips(settings: Settings) -> int:
    """Delete generated profile zips older than zip_ttl_seconds (by mtime)."""
    zdir = settings.data_dir / "zips"
    if not zdir.is_dir():
        return 0
    cutoff = time.time() - settings.zip_ttl_seconds
    removed = 0
    for entry in os.scandir(zdir):
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)
                removed += 1
        except OSError:
            logger.warning("could not remove stale zip %s", entry.path)
    return removed


async def _zip_ttl_loop(settings: Settings) -> None:
    while True:
        await asyncio.sleep(60)
        try:
            removed = _reap_zips(settings)
            if removed:
                logger.info("reaped %d expired profile zip(s)", removed)
        except Exception:  # noqa: BLE001
            logger.exception("zip ttl reaper failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logging.basicConfig(level=settings.log_level.upper())
    logging.getLogger("uvicorn.access").addFilter(_SuppressHealthAccessLogs())
    for path in (
        settings.downloads_dir,
        settings.data_dir / "archive",
        settings.data_dir / "profiles",
        settings.data_dir / "thumbnails",
        settings.data_dir / "zips",
        settings.cookies_path.parent,
    ):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("could not create path %s", path)

    # The downloads dir is the one path a misconfiguration can silently break: point DOWNLOADS_DIR
    # at a host path that was never bind-mounted and every job fails deep inside gallery-dl. Say so
    # loudly at startup — jobs also refuse to spawn (see JobManager._downloads_dir_problem).
    if not os.access(settings.downloads_dir, os.W_OK):
        logger.error(
            "downloads dir %s is missing or not writable by this user — downloads WILL fail. "
            "If it is a host path, check that it is bind-mounted into the container.",
            settings.downloads_dir,
        )

    app.state.cookie_store.load()
    gc_task = asyncio.create_task(_gc_loop(app.state.job_manager))
    zip_task = asyncio.create_task(_zip_ttl_loop(settings))
    logger.info("gallery-dl-web API ready (data_dir=%s)", settings.data_dir)
    try:
        yield
    finally:
        gc_task.cancel()
        zip_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await gc_task
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await zip_task


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="gallery-dl-web API", version="0.3.1", lifespan=lifespan)
    app.state.settings = settings
    app.state.cookie_store = CookieStore(settings.cookies_path)
    app.state.profile_store = ProfileStore(settings)
    app.state.job_manager = JobManager(settings, app.state.cookie_store, app.state.profile_store)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Accept"],
    )

    # /health at root (frontend proxy maps /health -> backend /health).
    app.include_router(routes_health.router)
    app.include_router(routes_jobs.router, prefix="/api")
    app.include_router(routes_settings.router, prefix="/api")
    app.include_router(routes_files.router, prefix="/api")
    app.include_router(routes_profiles.router, prefix="/api")
    return app
