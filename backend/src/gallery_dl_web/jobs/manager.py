"""JobManager — the asyncio orchestrator.

Responsibilities:
  * keep an in-memory registry of jobs (``JobState``);
  * spawn one isolated gallery-dl worker subprocess per job, throttled by a semaphore;
  * read the worker's JSON-lines stdout and fan each event out to SSE subscribers
    (and append to a bounded history deque for late joiners / reconnects);
  * always reach a terminal state, synthesizing a ``failed`` event if the worker dies silently;
  * periodically garbage-collect terminal jobs older than an hour.

State is in-memory and lost on restart — by design. Cross-run de-duplication is handled by
gallery-dl's SQLite ``archive``, not by this registry.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import time
from typing import Any

from gallery_dl_web.config import Settings
from gallery_dl_web.cookies.store import CookieStore
from gallery_dl_web.jobs.models import JobState, JobStatus
from gallery_dl_web.jobs.worker_runner import spawn_worker

logger = logging.getLogger(__name__)

_GC_INTERVAL_SECONDS = 300
_GC_MAX_AGE_SECONDS = 3600
_TERMINAL_EVENTS = frozenset({"completed", "failed"})


def _new_job_id() -> str:
    """Time-prefixed hex id — sortable, URL-safe, no extra dependency."""
    return f"{int(time.time()):x}{_token()}"


def _token() -> str:
    # secrets would do, but os.urandom avoids an import and is plenty here.
    import os

    return os.urandom(6).hex()


class JobManager:
    def __init__(self, settings: Settings, cookie_store: CookieStore) -> None:
        self._settings = settings
        self._cookies = cookie_store
        self._jobs: dict[str, JobState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._semaphore = asyncio.BoundedSemaphore(max(1, settings.max_concurrent_jobs))
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ creation

    async def create_job(
        self,
        url: str,
        platform: str,
        options: dict[str, Any] | None = None,
    ) -> str:
        job_id = _new_job_id()
        state = JobState(id=job_id, url=url, platform=platform, status=JobStatus.QUEUED)
        # Record "queued" directly into history (no subscribers yet; SSE replays it on connect).
        state.events.append({"type": "queued", "job_id": job_id, "url": url, "ts": time.time()})
        async with self._lock:
            self._jobs[job_id] = state
        task = asyncio.create_task(self._run_job(state, options or {}))
        self._tasks[job_id] = task
        logger.info("created job %s for %s (%s)", job_id, platform, url)
        return job_id

    async def wait_for(self, job_id: str) -> None:
        """Await a job's background task. Test helper; missing task returns immediately."""
        task = self._tasks.get(job_id)
        if task is not None:
            await task

    # ------------------------------------------------------------------ queries

    def get(self, job_id: str) -> JobState | None:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[JobState]:
        return sorted(self._jobs.values(), key=lambda s: s.created_at, reverse=True)

    def subscribe(self, state: JobState) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        state.subscribers.add(queue)
        return queue

    @staticmethod
    def unsubscribe(state: JobState, queue: asyncio.Queue[dict[str, Any]]) -> None:
        state.subscribers.discard(queue)

    # ------------------------------------------------------------------ run loop

    async def _run_job(self, state: JobState, options: dict[str, Any]) -> None:
        try:
            async with self._semaphore:
                state.status = JobStatus.RUNNING
                state.started_at = time.time()
                # The worker emits the authoritative "started" event (per the event contract);
                # we only flip the internal status here to avoid a duplicate on the wire.

                cookies = self._cookies.get_for_platform(state.platform)
                if not cookies:
                    await self._emit(
                        state,
                        {
                            "type": "failed",
                            "job_id": state.id,
                            "exit_status": 0,
                            "reason": "missing-cookies",
                            "message": (
                                f"No cookies configured for {state.platform}. "
                                "Add them in Settings before downloading."
                            ),
                            "ts": time.time(),
                        },
                    )
                    return

                await self._spawn_and_stream(state, options, cookies)
        except asyncio.CancelledError:
            logger.info("job %s cancelled", state.id)
            if not state.is_terminal:
                await self._emit(
                    state,
                    {
                        "type": "failed",
                        "job_id": state.id,
                        "exit_status": 2,
                        "reason": "cancelled",
                        "message": "job cancelled",
                        "ts": time.time(),
                    },
                )
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("runner error for job %s", state.id)
            if not state.is_terminal:
                await self._emit(
                    state,
                    {
                        "type": "failed",
                        "job_id": state.id,
                        "exit_status": 2,
                        "reason": "runner-error",
                        "message": str(exc),
                        "ts": time.time(),
                    },
                )
        finally:
            state.ended_at = time.time()
            self._tasks.pop(state.id, None)

    async def _spawn_and_stream(
        self,
        state: JobState,
        options: dict[str, Any],
        cookies: dict[str, Any] | str,
    ) -> None:
        payload = self._build_payload(state, options, cookies)
        python = self._settings.worker_python or sys.executable
        logger.info("spawning worker for job %s (%s)", state.id, state.platform)
        proc = await spawn_worker(python, payload)
        stdout = proc.stdout
        assert stdout is not None
        async for raw in stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("job %s: worker emitted non-JSON line: %s", state.id, line[:200])
                continue
            if not isinstance(event, dict) or "type" not in event:
                continue
            await self._emit(state, event)
        await proc.wait()

        if not state.is_terminal:
            # Worker exited without a terminal event (e.g. SIGKILL) — synthesize one.
            stderr = ""
            if proc.stderr is not None:
                try:
                    stderr = (await proc.stderr.read()).decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    stderr = ""
            await self._emit(
                state,
                {
                    "type": "failed",
                    "job_id": state.id,
                    "exit_status": proc.returncode or 0,
                    "reason": "worker-crash",
                    "message": (stderr.strip()[:500] or "worker exited without a terminal event"),
                    "ts": time.time(),
                },
            )

    def _build_payload(
        self,
        state: JobState,
        options: dict[str, Any],
        cookies: dict[str, Any] | str,
    ) -> dict[str, Any]:
        merged = dict(options)
        archive_dir = self._settings.data_dir / "archive"
        with contextlib.suppress(OSError):
            archive_dir.mkdir(parents=True, exist_ok=True)
        merged.setdefault("archive", str(archive_dir / f"{state.platform}.sqlite"))
        return {
            "job_id": state.id,
            "url": state.url,
            "platform": state.platform,
            "output_dir": str(self._settings.downloads_dir),
            "cookies": cookies,
            "options": merged,
            "preview": False,
        }

    async def _emit(self, state: JobState, event: dict[str, Any]) -> None:
        etype = event.get("type")
        state.events.append(event)
        if etype == "completed":
            state.status = JobStatus.COMPLETED
            state.final_summary = event
        elif etype == "failed":
            state.status = JobStatus.FAILED
            state.final_summary = event

        for sub in list(state.subscribers):
            try:
                sub.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer — drop it so SSE reconnects and replays history.
                state.subscribers.discard(sub)

    # ------------------------------------------------------------------ gc

    async def gc(self) -> int:
        """Drop terminal jobs older than the max age. Returns the number removed."""
        now = time.time()
        cutoff = now - _GC_MAX_AGE_SECONDS
        async with self._lock:
            stale = [
                jid
                for jid, s in self._jobs.items()
                if s.is_terminal and (s.ended_at or s.created_at) < cutoff
            ]
            for jid in stale:
                self._jobs.pop(jid, None)
        return len(stale)
