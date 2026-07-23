from __future__ import annotations

from gallery_dl_web.jobs.models import JobState, JobStatus


def test_to_summary_defaults() -> None:
    s = JobState(id="j1", url="https://ig/x", platform="instagram")
    summ = s.to_summary()
    assert summ["id"] == "j1"
    assert summ["status"] == "queued"
    assert summ["final_summary"] is None
    assert summ["started_at"] is None


def test_is_terminal() -> None:
    assert JobState(id="j", url="u", platform="instagram", status=JobStatus.COMPLETED).is_terminal
    assert JobState(id="j", url="u", platform="instagram", status=JobStatus.FAILED).is_terminal
    assert not JobState(id="j", url="u", platform="instagram", status=JobStatus.RUNNING).is_terminal
    assert not JobState(id="j", url="u", platform="instagram").is_terminal


def test_downloaded_paths_dedups_and_orders() -> None:
    s = JobState(id="j", url="u", platform="instagram")
    s.events.append({"type": "file", "event": "downloaded", "path": "/a/1.jpg"})
    s.events.append({"type": "file", "event": "skipped", "path": "/a/2.jpg"})
    s.events.append({"type": "file", "event": "downloaded", "path": "/a/1.jpg"})
    s.events.append({"type": "file", "event": "downloaded", "path": "/a/3.jpg"})
    assert s.downloaded_paths() == ["/a/1.jpg", "/a/3.jpg"]


def test_event_history_is_bounded() -> None:
    s = JobState(id="j", url="u", platform="instagram")
    for i in range(10_000):
        s.events.append({"i": i})
    assert len(s.events) <= 5000


def test_media_paths_includes_skipped_files() -> None:
    """Deriving the touched profile must not depend on anything being *newly* downloaded.

    A stopped job — or any re-run that found everything already archived — has skip events only,
    which would otherwise leave metadata.json unreconciled.
    """
    s = JobState(id="j", url="u", platform="instagram")
    s.events.append({"type": "file", "event": "skipped", "path": "/a/2.jpg"})
    s.events.append({"type": "file", "event": "downloaded", "path": "/a/1.jpg"})
    s.events.append({"type": "file", "event": "skipped", "path": "/a/2.jpg"})
    s.events.append({"type": "progress", "path": "/ignored"})
    assert s.media_paths() == ["/a/2.jpg", "/a/1.jpg"]
    assert s.downloaded_paths() == ["/a/1.jpg"]


def test_cancelled_is_terminal_but_paused_is_not() -> None:
    cancelled = JobState(id="j", url="u", platform="instagram", status=JobStatus.CANCELLED)
    paused = JobState(id="j", url="u", platform="instagram", status=JobStatus.PAUSED)
    assert cancelled.is_terminal and not cancelled.is_active
    assert paused.is_active and not paused.is_terminal


def test_summary_carries_live_counters_for_the_queue_page() -> None:
    s = JobState(id="j", url="u", platform="instagram")
    s.profile = "someone"
    s.downloaded, s.skipped, s.file_count = 7, 3, 10
    s.paused_at = 123.0
    summ = s.to_summary()
    assert summ["profile"] == "someone"
    assert (summ["downloaded"], summ["skipped"], summ["file_count"]) == (7, 3, 10)
    assert summ["paused_at"] == 123.0
