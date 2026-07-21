"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from fastapi import FastAPI

from gallery_dl_web.api.app import create_app
from gallery_dl_web.config import Settings
from gallery_dl_web.cookies.store import CookieStore
from gallery_dl_web.jobs.manager import JobManager

# A representative valid Facebook Netscape cookie block (c_user + xs = a real session).
FB_NETSCAPE = "\n".join(
    [
        "# Netscape HTTP Cookie File",
        "# This is a generated file!  Do not edit.",
        "#HttpOnly_.facebook.com\tTRUE\t/\tTRUE\t1900000000\tc_user\t123456789",
        "#HttpOnly_.facebook.com\tTRUE\t/\tTRUE\t1900000000\txs\t12:ABCdef",
        ".facebook.com\tTRUE\t/\tFALSE\t1900000000\tfr\tabcdef",
    ]
)


@pytest.fixture
def tmp_settings(tmp_path: str) -> Settings:
    import pathlib

    data = pathlib.Path(tmp_path) / "data"
    return Settings(
        data_dir=data,
        downloads_dir=data / "downloads",
        cookies_path=data / "cookies.json",
        cors_origins=["http://testserver"],
        max_concurrent_jobs=2,
        host="127.0.0.1",
        port=8000,
    )


@pytest.fixture
def app(tmp_settings: Settings) -> FastAPI:
    return create_app(tmp_settings)


class _FakeStdout:
    """Async-iterable of bytes lines, mimicking ``proc.stdout``."""

    def __init__(self, lines: list[str], delay: float = 0.0) -> None:
        self._lines = [ln.encode() for ln in lines]
        self._delay = delay

    def __aiter__(self) -> _FakeStdout:
        return self

    async def __anext__(self) -> bytes:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._lines:
            return self._lines.pop(0)
        raise StopAsyncIteration


class FakeProc:
    def __init__(self, lines: list[str], returncode: int = 0, delay: float = 0.0) -> None:
        self.stdout = _FakeStdout(lines, delay)
        self.stderr = None
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


@pytest.fixture
def fake_spawn() -> Callable[..., Callable[[str, dict[str, Any]], Awaitable[FakeProc]]]:
    """Factory: ``fake_spawn(lines, rc, delay)`` -> an async ``spawn_worker`` replacement."""

    def factory(
        lines: list[str], returncode: int = 0, delay: float = 0.0
    ) -> Callable[[str, dict[str, Any]], Awaitable[FakeProc]]:
        async def _spawn(python: str, payload: dict[str, Any]) -> FakeProc:  # noqa: ARG001
            return FakeProc(lines, returncode, delay)

        return _spawn

    return factory


@pytest.fixture
def cookie_store(app: FastAPI) -> CookieStore:
    # Return the SAME instance the app's JobManager reads, so tests that set cookies are visible
    # to job creation and HTTP endpoints.
    return cast(CookieStore, app.state.cookie_store)


@pytest.fixture
def job_manager(app: FastAPI) -> JobManager:
    return cast(JobManager, app.state.job_manager)
