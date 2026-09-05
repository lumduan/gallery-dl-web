"""``FileIndex`` — the walk that took the whole API down, and the three properties that fix it.

On 2026-09-05 a NAS-backed install with 427,009 files served `GET /api/files` by running `os.walk`
inline in an `async def`. That is not a slow endpoint, it is a global outage: a blocked event loop
serves nothing, so the container's own `/health` probe timed out and Docker marked the backend
unhealthy while it was working exactly as written.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest

from gallery_dl_web.files.index import FileIndex, scan


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    d = tmp_path / "downloads" / "instagram"
    d.mkdir(parents=True)
    for i in range(5):
        f = d / f"{i}.jpg"
        f.write_bytes(b"x" * (i + 1))
        os.utime(f, (1_000_000 + i * 60, 1_000_000 + i * 60))
    (d / "archive.sqlite").write_bytes(b"x")
    return tmp_path / "downloads"


async def test_the_walk_leaves_the_event_loop_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE regression test, measured on the loop itself rather than through a request.

    Two earlier shapes of this test both passed with the offload deleted, which is worse than no
    test at all:

    * *start the listing, sleep, then time `/health`* — the blocking walk ran to completion inside
      that very `sleep`, so the timer measured an idle loop afterwards;
    * *launch both and assert `/health` finishes first* — ordering through `httpx` is decided by how
      many await points each request has before it reaches the handler, not by the blocking.

    Both were checks whose success did not mean what it appeared to mean. This one counts how many
    times the loop got to run while the walk was in flight, which is the property itself: a walk on
    the loop yields zero ticks, a walk in a thread yields dozens.
    """

    def slow_scan(_downloads: Path) -> list[Any]:
        time.sleep(0.5)
        return []

    monkeypatch.setattr("gallery_dl_web.files.index.scan", slow_scan)

    index = FileIndex(tmp_path)
    ticks = 0
    task = asyncio.create_task(index.entries())
    while not task.done():
        await asyncio.sleep(0.01)
        ticks += 1
    await task

    assert ticks > 10, (
        f"the loop ran only {ticks} time(s) during a 0.5 s walk — it is blocked, "
        "which is what took `/health` down with it"
    )


async def test_concurrent_callers_walk_the_tree_once(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-flight, and it is part of the fix rather than an optimisation.

    Moving the walk into a thread removes the *accidental* serialisation the event-loop block used
    to provide. Without the lock, five page loads become five concurrent 427k-file NFS walks —
    strictly worse than the bug being fixed.
    """
    real = scan

    def slow_scan(downloads: Path) -> list[Any]:
        time.sleep(0.2)  # wide enough that all five callers are genuinely in flight together
        return real(downloads)

    monkeypatch.setattr("gallery_dl_web.files.index.scan", slow_scan)

    index = FileIndex(tree)
    results = await asyncio.gather(*(index.entries() for _ in range(5)))

    assert index.scans == 1, f"the tree was walked {index.scans} times"
    assert all(len(r) == 5 for r in results), "every caller still gets the full listing"


async def test_a_fresh_cache_is_not_rewalked(tree: Path) -> None:
    index = FileIndex(tree)
    await index.entries()
    await index.entries()
    assert index.scans == 1


async def test_an_expired_cache_is_rewalked(tree: Path) -> None:
    index = FileIndex(tree, ttl=0.0)
    await index.entries()
    await index.entries()
    assert index.scans == 2


async def test_invalidate_forces_a_rewalk(tree: Path) -> None:
    index = FileIndex(tree)
    await index.entries()
    index.invalidate()
    await index.entries()
    assert index.scans == 2


def test_scan_is_newest_first_and_hides_the_archive(tree: Path) -> None:
    entries = scan(tree)
    assert [e.name for e in entries] == ["4.jpg", "3.jpg", "2.jpg", "1.jpg", "0.jpg"]
    assert all(not e.name.endswith(".sqlite") for e in entries)
    assert {e.platform for e in entries} == {"instagram"}


def test_scan_of_a_missing_tree_is_empty_not_an_error(tmp_path: Path) -> None:
    assert scan(tmp_path / "nope") == []
