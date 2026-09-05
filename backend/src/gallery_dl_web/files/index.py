"""A cached, off-loop index of the downloads tree.

**Why this exists.** ``GET /api/files`` used to run ``os.walk`` directly inside its ``async def``
handler. On a NAS-backed install that is not a slow endpoint, it is a **global outage**: the walk
blocks the asyncio event loop, so every other request — a job's SSE stream, ``/api/jobs``, and the
container's own ``/health`` probe — stops being served until it finishes. Observed live on
2026-09-05 against 427,009 files over NFS: the main thread sat in uninterruptible disk sleep, the
5 s health probe timed out three times in a row, and Docker marked the backend unhealthy while it
was in fact working exactly as written.

Three things are needed, and only the first is obvious:

* **Off the loop.** ``asyncio.to_thread``, the same way ``ProfileStore.reconcile`` already does it.
* **Single-flight.** Moving the walk to a thread removes the *accidental* serialisation the block
  used to provide. Without a lock, five page loads become five concurrent 427k-file NFS walks —
  strictly worse than the bug being fixed. The lock is part of the fix, not an optimisation.
* **A short TTL.** A full walk costs tens of seconds no matter which thread it runs on, so a page
  that polls, or an operator who reloads, must not pay it every time.

The result is still a *complete* walk, because "newest first" cannot be answered without stating
everything. What the TTL buys is that it happens at most once per ``CACHE_TTL_SECONDS``.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from gallery_dl_web.schemas.files import FileEntry

# Not worth surfacing as a Settings field: it trades staleness against NFS load, and both ends of
# that trade are fine anywhere in the tens of seconds. A running job appends files continuously, so
# the listing is a moving target regardless of what this is set to.
CACHE_TTL_SECONDS = 30.0

# gallery-dl's own bookkeeping, never the operator's media.
HIDDEN_SUFFIXES = (".sqlite", ".sqlite-journal", ".sqlite-wal", ".sqlite-shm", ".db")


def scan(downloads: Path) -> list[FileEntry]:
    """Walk the tree and return every media file, newest first. **Blocking — call in a thread.**"""
    entries: list[FileEntry] = []
    if not downloads.exists():
        return entries
    for root, _dirs, files in os.walk(downloads):
        root_path = Path(root)
        for name in files:
            if name.lower().endswith(HIDDEN_SUFFIXES):
                continue
            full = root_path / name
            try:
                st = full.stat()
            except OSError:
                continue  # vanished mid-walk, or an unreadable mount point
            rel = full.relative_to(downloads)
            entries.append(
                FileEntry(
                    path=str(rel),
                    name=name,
                    size=st.st_size,
                    mtime=st.st_mtime,
                    platform=rel.parts[0] if rel.parts else "",
                )
            )
    entries.sort(key=lambda e: e.mtime, reverse=True)
    return entries


class FileIndex:
    """Serves the downloads listing from a short-lived cache, refreshed off the event loop.

    One instance per app, on ``app.state``, so a fresh app per test gets a fresh cache — module
    level state would leak a previous test's tree into the next one.
    """

    def __init__(self, downloads: Path, ttl: float = CACHE_TTL_SECONDS) -> None:
        self._downloads = downloads
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._entries: list[FileEntry] | None = None
        self._scanned_at = 0.0
        # Test/diagnostic counter: proves the lock actually collapsed concurrent callers into one
        # walk, which is the whole point and is otherwise invisible from the outside.
        self.scans = 0

    def invalidate(self) -> None:
        """Drop the cache so the next read re-walks."""
        self._entries = None

    async def entries(self) -> list[FileEntry]:
        """Every media file, newest first. At most one walk per TTL, never on the event loop."""
        if self._fresh():
            return self._entries  # type: ignore[return-value]
        async with self._lock:
            # Re-check inside the lock: whoever held it may have just refreshed for us, and paying
            # for a second identical walk is exactly what the lock is here to prevent.
            if self._fresh():
                return self._entries  # type: ignore[return-value]
            entries = await asyncio.to_thread(scan, self._downloads)
            self._entries = entries
            self._scanned_at = time.monotonic()
            self.scans += 1
            return entries

    def _fresh(self) -> bool:
        return self._entries is not None and (time.monotonic() - self._scanned_at) < self._ttl
