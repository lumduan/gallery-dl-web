"""FastAPI dependency providers.

Singletons live on ``app.state`` (created in ``create_app``) so each app instance — including a
fresh one per test — gets isolated state. Dependencies read from the request's app.
"""

from __future__ import annotations

from fastapi import Request

from gallery_dl_web.config import Settings
from gallery_dl_web.cookies.store import CookieStore
from gallery_dl_web.jobs.manager import JobManager


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_cookie_store(request: Request) -> CookieStore:
    return request.app.state.cookie_store  # type: ignore[no-any-return]


def get_job_manager(request: Request) -> JobManager:
    return request.app.state.job_manager  # type: ignore[no-any-return]
