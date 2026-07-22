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
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from gallery_dl_web.config import Settings
from gallery_dl_web.cookies.store import CookieStore
from gallery_dl_web.jobs.models import JobState, JobStatus
from gallery_dl_web.jobs.worker_runner import spawn_worker
from gallery_dl_web.profiles.store import ProfileStore
from gallery_dl_web.profiles.urls import extract_username

logger = logging.getLogger(__name__)

_GC_INTERVAL_SECONDS = 300
_GC_MAX_AGE_SECONDS = 3600
_TERMINAL_EVENTS = frozenset({"completed", "failed"})
# How much worker stderr to keep for the failure message (gallery-dl can be very chatty).
_STDERR_TAIL_LINES = 50
_STDERR_TAIL_CHARS = 2000
# Let the concurrent stderr drain catch up before quoting it in a failure message.
_STDERR_SETTLE_SECONDS = 0.25


def _new_job_id() -> str:
    """Time-prefixed hex id — sortable, URL-safe, no extra dependency."""
    return f"{int(time.time()):x}{_token()}"


def _token() -> str:
    # secrets would do, but os.urandom is plenty here.
    return os.urandom(6).hex()


def _tail_text(lines: deque[str]) -> str:
    """Render captured worker stderr for a failure message, newest-biased and length-bounded."""
    if not lines:
        return ""
    text = "\n".join(lines)
    if len(text) > _STDERR_TAIL_CHARS:
        text = "…" + text[-_STDERR_TAIL_CHARS:]
    return f"worker output:\n{text}"


class JobManager:
    def __init__(
        self,
        settings: Settings,
        cookie_store: CookieStore,
        profile_store: ProfileStore | None = None,
    ) -> None:
        self._settings = settings
        self._cookies = cookie_store
        self._profiles = profile_store
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

                problem = self._downloads_dir_problem(state.platform)
                if problem is not None:
                    await self._emit(
                        state,
                        {
                            "type": "failed",
                            "job_id": state.id,
                            "exit_status": 0,
                            "reason": "downloads-dir-unwritable",
                            "message": problem,
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
            if self._profiles is not None and state.is_terminal:
                await self._reconcile_profile(state)

    def _downloads_dir_problem(self, platform: str | None = None) -> str | None:
        """Return a human-readable reason the downloads dir is unusable, or None if it's fine.

        Checked BEFORE spawning: gallery-dl would otherwise fail on every single file, flooding
        stderr and looking like a mysterious stall (that is exactly how this was first hit — a
        DOWNLOADS_DIR pointing at a host path that was never bind-mounted into the container).

        The per-platform subdirectory is checked too. A writable root with an unwritable child is
        the nastier variant: reads succeed, so already-archived files report ``skipped`` and the
        job looks healthy right up until every actual download fails. That happens whenever the
        tree was created by another user — e.g. copied in as root onto an NFS share with
        ``root_squash``, which lands it as ``nobody:nogroup`` mode 0755.
        """
        downloads = self._settings.downloads_dir
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return (
                f"Downloads directory {downloads} cannot be created ({exc.strerror}). "
                "If this is a host path, check that it is bind-mounted into the container "
                "and writable by the app user."
            )
        if not os.access(downloads, os.W_OK):
            return (
                f"Downloads directory {downloads} is not writable by the app user. "
                "Check the bind mount and its permissions."
            )
        if platform:
            target = downloads / platform
            # Only meaningful if it already exists; gallery-dl creates it otherwise.
            if target.is_dir() and not os.access(target, os.W_OK):
                st = target.stat()
                return (
                    f"{target} exists but is not writable by the app user "
                    f"(uid {os.geteuid()}); it is owned by uid {st.st_uid} with mode "
                    f"{oct(st.st_mode)[-4:]}. Existing files can still be read, so downloads "
                    "would fail one-by-one while already-archived files report as skipped. "
                    "Fix the ownership/permissions of that directory."
                )
        return None

    async def _reconcile_profile(self, state: JobState) -> None:
        """After a job, rebuild the affected profile's metadata.json. Never fails the job."""
        paths = state.downloaded_paths()
        if not paths or self._profiles is None:
            return
        try:
            rel = Path(paths[0]).resolve().relative_to(self._settings.downloads_dir.resolve())
            platform, name = rel.parts[0], rel.parts[1]
        except (ValueError, IndexError):
            return
        if platform not in {"instagram", "facebook"} or not name:
            return
        try:
            await self._profiles.reconcile(platform, name, archive_path=state.archive_path)
        except Exception:  # noqa: BLE001
            logger.exception("metadata reconcile failed for %s/%s", platform, name)

    async def _spawn_and_stream(
        self,
        state: JobState,
        options: dict[str, Any],
        cookies: dict[str, Any] | str,
    ) -> None:
        """Spawn the worker, stream events with stall deadlines, retry on stall/crash.

        Retry budget depends on how far the job got: a job that has never produced a file timed out
        in *warm-up* (gallery-dl still enumerating), and repeating that just repeats the same slow
        walk from zero — the per-profile archive can only resume what was actually downloaded. So
        warm-up gets ``stall_warmup_max_retries``; once files are flowing, ``stall_max_retries``.
        """
        payload = self._build_payload(state, options, cookies)
        python = self._settings.worker_python or sys.executable

        attempt = 0
        while True:
            logger.info(
                "spawning worker for job %s (%s, attempt %d)", state.id, state.platform, attempt + 1
            )
            proc = await spawn_worker(python, payload)
            stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
            drain = asyncio.create_task(self._drain_stderr(state, proc, stderr_tail))
            outcome = await self._stream_with_stall(state, proc, attempt, stderr_tail)

            if outcome == "terminal":
                await self._stop_drain(drain)
                await self._reap(proc)
                return

            stalled = outcome == "stalled"
            warmup = state.file_count == 0
            if stalled:
                await self._kill_worker(proc)
            else:  # eof: worker exited without a terminal event
                await self._reap(proc)
            await self._stop_drain(drain)

            max_retries = (
                max(0, self._settings.stall_warmup_max_retries)
                if warmup
                else max(0, self._settings.stall_max_retries)
            )
            if attempt < max_retries:
                if stalled:
                    since = time.time() - state.last_file_ts if state.last_file_ts else None
                    await self._emit(
                        state,
                        {
                            "type": "stalled",
                            "job_id": state.id,
                            "attempt": attempt + 1,
                            "threshold": self._stall_threshold(state, attempt),
                            "since_last_file": since,
                            "phase": "warmup" if warmup else "download",
                            "ts": time.time(),
                        },
                    )
                await self._emit(
                    state,
                    {
                        "type": "retrying",
                        "job_id": state.id,
                        "attempt": attempt + 2,
                        "reason": "stalled" if stalled else "worker-exited",
                        "ts": time.time(),
                    },
                )
                attempt += 1
                continue

            # Retries exhausted -> terminal failure. Surface the worker's stderr tail: it usually
            # carries gallery-dl's real error (auth wall, permission denied, rate limit).
            reason, message = self._failure_reason(stalled, warmup)
            tail = _tail_text(stderr_tail)
            await self._emit(
                state,
                {
                    "type": "failed",
                    "job_id": state.id,
                    "exit_status": 0,
                    "reason": reason,
                    "message": f"{message}\n{tail}" if tail else message,
                    "downloaded": state.downloaded,
                    "skipped": state.skipped,
                    "ts": time.time(),
                },
            )
            return

    @staticmethod
    def _failure_reason(stalled: bool, warmup: bool) -> tuple[str, str]:
        if not stalled:
            return "worker-crash", "worker exited without a terminal event"
        if warmup:
            return (
                "no-progress",
                "gallery-dl never produced a file before the warm-up deadline. The profile may be "
                "private or restricted, the session cookies may be stale, or the platform may be "
                "throttling. See the details below.",
            )
        return "stalled", "download stalled (no progress within threshold); retries exhausted"

    async def _drain_stderr(
        self,
        state: JobState,
        proc: asyncio.subprocess.Process,
        tail: deque[str],
    ) -> None:
        """Continuously read the worker's stderr into ``tail``.

        This MUST run for the lifetime of the process: stderr is a pipe, and once ~64 KB of
        undrained output accumulates the worker BLOCKS on write. Its stdout then goes silent and
        the stall detector misreports a wedged process as a stalled download.
        """
        stream = proc.stderr
        if stream is None:
            return
        while True:
            try:
                raw = await stream.readline()
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception:  # noqa: BLE001 — draining must never break the job
                logger.debug("job %s: stderr drain ended early", state.id, exc_info=True)
                return
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                tail.append(line)
                logger.debug("job %s worker stderr: %s", state.id, line)

    @staticmethod
    async def _stop_drain(task: asyncio.Task[None]) -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _stream_with_stall(
        self,
        state: JobState,
        proc: asyncio.subprocess.Process,
        attempt: int,
        stderr_tail: deque[str] | None = None,
    ) -> str:
        """Read worker stdout under two deadlines. Returns 'terminal' | 'stalled' | 'eof'.

        * **liveness** — no line at all (not even a heartbeat) within ``stall_liveness_seconds``
          means the process is wedged, not slow.
        * **progress** — no ``file`` event within ``_stall_threshold``. Heartbeats deliberately do
          NOT reset this clock; only real download progress does.

        Splitting the two is the whole point: gallery-dl is legitimately silent for minutes while
        it enumerates a profile, so a single "any output" deadline killed healthy jobs.
        """
        stdout = proc.stdout
        assert stdout is not None
        started = time.time()
        liveness = max(1.0, self._settings.stall_liveness_seconds)
        last_line_ts = started
        heartbeat_seen = False
        while True:
            now = time.time()
            progress_deadline = self._stall_threshold(state, attempt)
            since_progress = now - (state.last_activity_ts or started)
            if since_progress >= progress_deadline:
                return "stalled"
            # Wake at whichever deadline comes first so neither can overshoot.
            until_liveness = liveness - (now - last_line_ts)
            until_progress = progress_deadline - since_progress
            wait = max(0.05, min(until_liveness, until_progress))
            try:
                raw = await asyncio.wait_for(stdout.readline(), timeout=wait)
            except TimeoutError:
                # Only treat silence as a wedge once we know this worker DOES send heartbeats —
                # otherwise the progress deadline above is the sole authority.
                if heartbeat_seen and time.time() - last_line_ts >= liveness:
                    logger.warning(
                        "job %s: no worker output for %.0fs despite heartbeats; assuming wedged",
                        state.id,
                        liveness,
                    )
                    return "stalled"
                continue
            if not raw:  # EOF
                return "eof"
            last_line_ts = time.time()
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
            etype = event.get("type")
            if etype == "progress":
                continue  # the manager owns job-level progress (monotonic across retries)
            if etype == "heartbeat":
                heartbeat_seen = True
                await self._emit(state, event)  # liveness only — does NOT reset the progress clock
                continue
            if etype == "prepare":
                # Real work: gallery-dl resolved a file. Counts as progress even if no `file`
                # event follows (filtered items, or a phase that yields nothing downloadable).
                state.last_activity_ts = time.time()
                await self._emit(state, event)
                continue
            if etype == "file":
                self._record_file(state, event)
                await self._emit(state, event)
                await self._emit(state, self._job_progress(state))
                continue
            if etype in ("completed", "failed"):
                event["downloaded"] = state.downloaded
                event["skipped"] = state.skipped
                if etype == "failed" and stderr_tail is not None:
                    # gallery-dl reports *what* failed via its exit-status bitmask but the *why*
                    # (auth wall, 404, rate limit) only ever reaches stderr. Attach it or the UI
                    # shows a bare "dl-failed". Yield briefly first: the drain task runs
                    # concurrently and may still have buffered lines to consume.
                    await asyncio.sleep(_STDERR_SETTLE_SECONDS)
                    tail = _tail_text(stderr_tail)
                    if tail:
                        existing = str(event.get("message") or "").strip()
                        event["message"] = f"{existing}\n{tail}" if existing else tail
                await self._emit(state, event)
                return "terminal"
            await self._emit(state, event)

    def _record_file(self, state: JobState, event: dict[str, Any]) -> None:
        ts = float(event.get("ts") or time.time())
        if state.first_file_ts is None:
            state.first_file_ts = ts
        state.last_file_ts = ts
        state.last_activity_ts = ts
        state.file_count += 1
        if event.get("event") == "skipped":
            state.skipped += 1
        else:
            state.downloaded += 1

    def _job_progress(self, state: JobState) -> dict[str, Any]:
        return {
            "type": "progress",
            "job_id": state.id,
            "downloaded": state.downloaded,
            "skipped": state.skipped,
            "failed": 0,
            "ts": time.time(),
        }

    def _stall_threshold(self, state: JobState, attempt: int) -> float:
        """Deadline for the next ``file`` event.

        Until gallery-dl shows ANY activity (a ``prepare`` or a ``file``) this is the WARM-UP
        budget: it is walking the profile and emits nothing, which for Instagram alone means 6-12s
        of sleep per paginated request. Using the steady-state floor here killed healthy jobs
        before they ever downloaded anything.

        Once activity starts, the adaptive rule applies:
        ``clamp(floor*backoff**attempt, multiplier*avg_inter_file, cap)``.
        """
        s = self._settings
        if state.last_activity_ts is None:
            return min(s.stall_warmup_seconds * (s.stall_backoff**attempt), s.stall_cap_seconds)
        floor = s.stall_floor_seconds * (s.stall_backoff**attempt)
        avg = 0.0
        if (
            state.file_count > 1
            and state.first_file_ts is not None
            and state.last_file_ts is not None
        ):
            avg = (state.last_file_ts - state.first_file_ts) / (state.file_count - 1)
        threshold = max(floor, s.stall_multiplier * avg) if avg > 0 else floor
        return min(threshold, s.stall_cap_seconds)

    async def _kill_worker(self, proc: asyncio.subprocess.Process) -> None:
        """SIGTERM, escalate to SIGKILL after the grace period."""
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._settings.stall_kill_grace_seconds)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):  # noqa: BLE001
                await proc.wait()

    async def _reap(self, proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(Exception):  # noqa: BLE001
            await proc.wait()

    def _build_payload(
        self,
        state: JobState,
        options: dict[str, Any],
        cookies: dict[str, Any] | str,
    ) -> dict[str, Any]:
        merged = dict(options)
        archive_dir = self._settings.data_dir / "archive"
        # Per-profile archive (so a profile can be deleted + re-downloaded cleanly). Falls back to
        # the shared per-platform archive when the URL exposes no username.
        username = extract_username(state.url, state.platform)
        if username:
            archive_subdir = archive_dir / state.platform
            with contextlib.suppress(OSError):
                archive_subdir.mkdir(parents=True, exist_ok=True)
            merged.setdefault("archive", str(archive_subdir / f"{username}.sqlite"))
            merged.setdefault("include_avatar", True)
        else:
            with contextlib.suppress(OSError):
                archive_dir.mkdir(parents=True, exist_ok=True)
            merged.setdefault("archive", str(archive_dir / f"{state.platform}.sqlite"))
        state.archive_path = merged.get("archive")
        return {
            "job_id": state.id,
            "url": state.url,
            "platform": state.platform,
            "output_dir": str(self._settings.downloads_dir),
            "cookies": cookies,
            "options": merged,
            "http_timeout_seconds": self._settings.http_timeout_seconds,
            "heartbeat_seconds": self._settings.heartbeat_seconds,
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
