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
    COMPLETED = "completed"
    FAILED = "failed"


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

    @property
    def is_terminal(self) -> bool:
        return self.status in (JobStatus.COMPLETED, JobStatus.FAILED)

    def to_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "platform": self.platform,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "final_summary": self.final_summary,
        }

    def downloaded_paths(self) -> list[str]:
        """Distinct on-disk paths of successfully downloaded files, in arrival order."""
        seen: set[str] = set()
        paths: list[str] = []
        for event in self.events:
            if event.get("type") == "file" and event.get("event") == "downloaded":
                path = event.get("path")
                if path and path not in seen:
                    seen.add(path)
                    paths.append(path)
        return paths
