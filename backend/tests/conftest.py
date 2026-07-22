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
    """Async-iterable of bytes lines, mimicking ``proc.stdout``.

    ``delay`` applies uniformly; ``delays`` gives a per-line schedule (the last value repeats),
    which is how "slow to produce the first file, then steady" extractions are modelled.
    """

    def __init__(
        self, lines: list[str], delay: float = 0.0, delays: list[float] | None = None
    ) -> None:
        self._lines = [ln.encode() for ln in lines]
        self._delay = delay
        self._delays = list(delays) if delays else None
        self._index = 0

    def _next_delay(self) -> float:
        if self._delays:
            i = min(self._index, len(self._delays) - 1)
            return self._delays[i]
        return self._delay

    def __aiter__(self) -> _FakeStdout:
        return self

    async def __anext__(self) -> bytes:
        line = await self.readline()
        if not line:
            raise StopAsyncIteration
        return line

    async def readline(self) -> bytes:
        # The manager reads worker stdout via StreamReader.readline(); mimic it (b"" = EOF).
        delay = self._next_delay()
        self._index += 1
        if delay:
            await asyncio.sleep(delay)
        if self._lines:
            return self._lines.pop(0)
        return b""


class FakeProc:
    def __init__(
        self,
        lines: list[str],
        returncode: int = 0,
        delay: float = 0.0,
        stderr_lines: list[str] | None = None,
        delays: list[float] | None = None,
    ) -> None:
        self.stdout = _FakeStdout(lines, delay, delays)
        # The manager drains stderr concurrently; give it a real stream so that path is exercised.
        self.stderr = _FakeStdout(stderr_lines or [])
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


@pytest.fixture
def fake_spawn() -> Callable[..., Callable[[str, dict[str, Any]], Awaitable[FakeProc]]]:
    """Factory: ``fake_spawn(lines, rc, delay)`` -> an async ``spawn_worker`` replacement."""

    def factory(
        lines: list[str],
        returncode: int = 0,
        delay: float = 0.0,
        stderr_lines: list[str] | None = None,
        delays: list[float] | None = None,
    ) -> Callable[[str, dict[str, Any]], Awaitable[FakeProc]]:
        async def _spawn(python: str, payload: dict[str, Any]) -> FakeProc:  # noqa: ARG001
            return FakeProc(lines, returncode, delay, stderr_lines, delays)

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
