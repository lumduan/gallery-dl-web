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
