"""Job state model — in-memory only (the gallery-dl SQLite archive handles cross-run dedup)."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    # Deliberately stopped by the operator. Terminal, but NOT a failure — the UI must not render
    # it red, and the files fetched before the stop are kept and reconciled into metadata.json.
    CANCELLED = "cancelled"


# Bounded so a runaway job can't consume unbounded memory; late SSE joiners still see full history.
_EVENT_HISTORY = 5000


@dataclass
class JobState:
    id: str
    url: str
    platform: str
    status: JobStatus = JobStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=_EVENT_HISTORY))
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    final_summary: dict[str, Any] | None = None
    # Per-profile archive path used for this job (so delete can remove it even though gallery-dl's
    # {username} folder name != the URL-derived archive key). Set in _build_payload.
    archive_path: str | None = None
    # Job-level (cross-retry) file tracking for the adaptive stall detector + monotonic progress.
    downloaded: int = 0
    skipped: int = 0
    first_file_ts: float | None = None
    last_file_ts: float | None = None
    file_count: int = 0
    # Last sign that gallery-dl is actively working through files — a `prepare` counts, not just a
    # completed `file`. Observed in the wild: 90 prepares in 60s with no file event (items resolved
    # but filtered out), which a file-only clock reads as "stalled" and kills a healthy job.
    last_activity_ts: float | None = None

    # ---------------------------------------------------------------- operator control
    # Set by JobManager.pause/resume/cancel from an HTTP handler; read by the job's own task.
    # Safe without a lock: asyncio is single-threaded and each flag is set without an await.
    pause_requested: bool = False
    cancel_requested: bool = False
    paused_at: float | None = None
    # Set by resume() (and by cancel(), so a paused read loop unblocks); awaited by the read loop.
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Set by every control action so the read loop's wait wakes at once instead of at its next
    # deadline (up to stall_liveness_seconds away — a user clicking Resume would see a dead button).
    control_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Whether this job currently holds a concurrency slot. Load-bearing: pause() hands the slot back
    # early so a queued profile can start, and _run_job's finally releases it again — a
    # BoundedSemaphore raises ValueError on an over-release, so both paths check this flag.
    holds_slot: bool = False
    # Display name for the queue UI (URL-derived up front, replaced by gallery-dl's actual folder
    # name once a file lands — the two differ, see profiles/urls.py).
    profile: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)

    @property
    def is_active(self) -> bool:
        """Queued, running, or paused — i.e. it still occupies the queue UI."""
        return not self.is_terminal

    def to_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "platform": self.platform,
            "profile": self.profile,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "paused_at": self.paused_at,
            # Live counters so the queue page can poll one endpoint instead of one SSE per job.
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "file_count": self.file_count,
            "last_activity_ts": self.last_activity_ts,
            "final_summary": self.final_summary,
        }

    def downloaded_paths(self) -> list[str]:
        """Distinct on-disk paths of successfully downloaded files, in arrival order."""
        return self._file_paths(downloaded_only=True)

    def media_paths(self) -> list[str]:
        """Distinct paths from ANY file event — downloaded *or* skipped.

        Used only to derive which profile a job touched. ``downloaded_paths`` misses the common
        case of a run whose files were all already in the archive (every event is ``skipped``),
        which would leave metadata.json unreconciled — very visible when an operator stops a job.
        The zip route deliberately keeps using ``downloaded_paths``: it must ship only new files.
        """
        return self._file_paths(downloaded_only=False)

    def _file_paths(self, *, downloaded_only: bool) -> list[str]:
        seen: set[str] = set()
        paths: list[str] = []
        for event in self.events:
            if event.get("type") != "file":
                continue
            if downloaded_only and event.get("event") != "downloaded":
                continue
            path = event.get("path")
            if path and path not in seen:
                seen.add(path)
                paths.append(path)
        return paths
