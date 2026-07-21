"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from gallery_dl_web.api import (
    routes_files,
    routes_health,
    routes_jobs,
    routes_settings,
)
from gallery_dl_web.config import Settings, get_settings
from gallery_dl_web.cookies.store import CookieStore
from gallery_dl_web.jobs.manager import JobManager

logger = logging.getLogger(__name__)


async def _gc_loop(manager: JobManager) -> None:
    while True:
        await asyncio.sleep(300)
        try:
            removed = await manager.gc()
            if removed:
                logger.info("gc removed %d terminal job(s)", removed)
        except Exception:  # noqa: BLE001
            logger.exception("job gc failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logging.basicConfig(level=settings.log_level.upper())
    for path in (
        settings.downloads_dir,
        settings.data_dir / "archive",
        settings.cookies_path.parent,
    ):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("could not create path %s", path)

    app.state.cookie_store.load()
    gc_task = asyncio.create_task(_gc_loop(app.state.job_manager))
    logger.info("gallery-dl-web API ready (data_dir=%s)", settings.data_dir)
    try:
        yield
    finally:
        gc_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await gc_task


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="gallery-dl-web API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.cookie_store = CookieStore(settings.cookies_path)
    app.state.job_manager = JobManager(settings, app.state.cookie_store)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Content-Type", "Accept"],
    )

    # /health at root (frontend proxy maps /health -> backend /health).
    app.include_router(routes_health.router)
    app.include_router(routes_jobs.router, prefix="/api")
    app.include_router(routes_settings.router, prefix="/api")
    app.include_router(routes_files.router, prefix="/api")
    return app
