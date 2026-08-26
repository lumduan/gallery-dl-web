"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
import signal
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

    ``suspended`` models SIGSTOP: a stopped process emits nothing at all until it is continued.
    Without it a "paused" fake worker keeps streaming and races the test to completion.
    """

    def __init__(
        self, lines: list[str], delay: float = 0.0, delays: list[float] | None = None
    ) -> None:
        self._lines = [ln.encode() for ln in lines]
        self._delay = delay
        self._delays = list(delays) if delays else None
        self._index = 0
        self.suspended = False

    def append_lines(self, lines: list[str]) -> None:
        """Queue more output on an already-running stream (a worker's SIGTERM flush)."""
        self._lines.extend(ln.encode() for ln in lines)

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
        while self.suspended:  # SIGSTOPed: produce nothing until SIGCONT
            await asyncio.sleep(0.01)
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
        on_terminate: list[str] | None = None,
    ) -> None:
        self.stdout = _FakeStdout(lines, delay, delays)
        # Lines the worker writes only after SIGTERM.
        self._on_terminate = list(on_terminate or [])
        # The manager drains stderr concurrently; give it a real stream so that path is exercised.
        self.stderr = _FakeStdout(stderr_lines or [])
        self.returncode = returncode
        self.pid = 4242
        self.terminated = False
        self.killed = False
        # Every signal the manager sent, in order — pause/resume/stop assert on this. The ordering
        # matters: a SIGSTOPed process never handles SIGTERM, so SIGCONT must come first.
        self.signals: list[int] = []

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        if sig == signal.SIGSTOP:
            self.stdout.suspended = True
        elif sig == signal.SIGCONT:
            self.stdout.suspended = False

    def terminate(self) -> None:
        self.terminated = True
        self.signals.append(signal.SIGTERM)
        # A real worker installs a SIGTERM handler that flushes its pacing ring buffer to stdout on
        # the way out. Model that, because the manager has to still be reading when it lands --
        # see test_a_cancel_captures_the_workers_parting_telemetry.
        if self._on_terminate:
            self.stdout.append_lines(self._on_terminate)
            self._on_terminate = []

    def kill(self) -> None:
        self.killed = True
        self.signals.append(signal.SIGKILL)

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
        on_terminate: list[str] | None = None,
    ) -> Callable[[str, dict[str, Any]], Awaitable[FakeProc]]:
        async def _spawn(python: str, payload: dict[str, Any]) -> FakeProc:  # noqa: ARG001
            return FakeProc(lines, returncode, delay, stderr_lines, delays, on_terminate)

        return _spawn

    return factory


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Safety net: a test must never launch a real gallery-dl worker (or touch the network).

    Before anonymous mode there was an accidental guard — a job with no cookies failed in
    ``_run_job`` before ``spawn_worker`` was reached, so a test that created a job without
    configuring cookies could not spawn anything. That pre-flight is gone by design: no cookies now
    means "run logged-out", which reaches the spawn. This restores the guarantee explicitly.

    Tests that install their own ``spawn_worker`` via ``fake_spawn`` still win — monkeypatch applies
    theirs after this one.
    """
    import json as _json

    from gallery_dl_web.jobs import manager as _mgr

    async def _spawn(python: str, payload: dict[str, Any]) -> FakeProc:  # noqa: ARG001
        return FakeProc(
            [_json.dumps({"type": "completed", "exit_status": 0, "downloaded": 0, "skipped": 0})]
        )

    monkeypatch.setattr(_mgr, "spawn_worker", _spawn)


@pytest.fixture
def capture_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Callable[[str, dict[str, Any]], Awaitable[FakeProc]]], list[dict[str, Any]]]:
    """Install a ``spawn_worker`` and return the list it records each payload into.

    The payload is the only place ``cookies`` / ``anonymous`` are observable — they never reach
    argv, an API response, or a log line.
    """

    def install(
        inner: Callable[[str, dict[str, Any]], Awaitable[FakeProc]],
    ) -> list[dict[str, Any]]:
        from gallery_dl_web.jobs import manager as _mgr

        seen: list[dict[str, Any]] = []

        async def _spawn(python: str, payload: dict[str, Any]) -> FakeProc:
            seen.append(payload)
            return await inner(python, payload)

        monkeypatch.setattr(_mgr, "spawn_worker", _spawn)
        return seen

    return install


@pytest.fixture
def cookie_store(app: FastAPI) -> CookieStore:
    # Return the SAME instance the app's JobManager reads, so tests that set cookies are visible
    # to job creation and HTTP endpoints.
    return cast(CookieStore, app.state.cookie_store)


@pytest.fixture
def job_manager(app: FastAPI) -> JobManager:
    return cast(JobManager, app.state.job_manager)
