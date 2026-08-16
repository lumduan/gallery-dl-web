from __future__ import annotations

import json
import time

from gallery_dl_web.jobs import manager as mgr_mod
from gallery_dl_web.jobs.models import JobState, JobStatus


async def test_no_cookies_falls_back_to_anonymous(job_manager, fake_spawn, capture_spawn) -> None:
    # No cookies configured -> run logged-out rather than refusing. gallery-dl reaches public
    # content without a session; if it does hit a wall, _annotate_failure says login-required.
    seen = capture_spawn(
        fake_spawn(
            [json.dumps({"type": "completed", "exit_status": 0, "downloaded": 0, "skipped": 0})]
        )
    )
    jid = await job_manager.create_job("https://instagram.com/p/x/", "instagram")
    await job_manager.wait_for(jid)
    state = job_manager.get(jid)
    assert state is not None
    assert state.status is JobStatus.COMPLETED
    assert state.anonymous is True
    assert seen[0]["anonymous"] is True
    assert seen[0]["cookies"] is None


async def test_explicit_anonymous_ignores_stored_cookies(
    job_manager, cookie_store, fake_spawn, capture_spawn
) -> None:
    # Opting in with cookies on file must not send them — the point is to spare a real session.
    cookie_store.update(ig_sessionid="SID")
    seen = capture_spawn(
        fake_spawn(
            [json.dumps({"type": "completed", "exit_status": 0, "downloaded": 0, "skipped": 0})]
        )
    )
    jid = await job_manager.create_job(
        "https://instagram.com/p/x/", "instagram", {"anonymous": True}
    )
    await job_manager.wait_for(jid)
    assert seen[0]["anonymous"] is True
    assert seen[0]["cookies"] is None
    # The flag is consumed by the manager; config_builder only reads known option keys, so leaving
    # it in `options` would be dead weight that still shows up in a payload dump.
    assert "anonymous" not in seen[0]["options"]


async def test_stored_cookies_are_used_when_not_opting_out(
    job_manager, cookie_store, fake_spawn, capture_spawn
) -> None:
    cookie_store.update(ig_sessionid="SID")
    seen = capture_spawn(
        fake_spawn(
            [json.dumps({"type": "completed", "exit_status": 0, "downloaded": 0, "skipped": 0})]
        )
    )
    jid = await job_manager.create_job("https://instagram.com/p/x/", "instagram")
    await job_manager.wait_for(jid)
    state = job_manager.get(jid)
    assert state is not None
    assert state.anonymous is False
    assert seen[0]["anonymous"] is False
    assert seen[0]["cookies"] == {"sessionid": "SID"}


async def test_completed_flow_and_subscriber_fanout(
    job_manager, cookie_store, fake_spawn, monkeypatch
) -> None:
    cookie_store.update(ig_sessionid="SID")
    monkeypatch.setattr(
        mgr_mod,
        "spawn_worker",
        fake_spawn(
            [
                json.dumps({"type": "started", "url": "u"}),
                json.dumps(
                    {
                        "type": "file",
                        "event": "downloaded",
                        "path": "/out/a.jpg",
                        "filename": "a.jpg",
                    }
                ),
                json.dumps({"type": "progress", "downloaded": 1, "skipped": 0, "failed": 0}),
                json.dumps({"type": "completed", "exit_status": 0, "downloaded": 1, "skipped": 0}),
            ]
        ),
    )

    jid = await job_manager.create_job("https://instagram.com/p/x/", "instagram")
    state = job_manager.get(jid)
    assert state is not None
    queue = job_manager.subscribe(state)
    await job_manager.wait_for(jid)

    assert state.status is JobStatus.COMPLETED
    assert state.is_terminal
    received: list[dict] = []
    while not queue.empty():
        received.append(queue.get_nowait())
    types = [e["type"] for e in received]
    assert "started" in types
    assert "completed" in types
    # History (for late SSE joiners) contains the full stream.
    assert any(e.get("type") == "completed" for e in state.events)


async def test_worker_crash_synthesizes_failed(
    job_manager, cookie_store, fake_spawn, monkeypatch
) -> None:
    cookie_store.update(ig_sessionid="SID")
    # Worker emits a non-terminal line then exits without a terminal event.
    monkeypatch.setattr(
        mgr_mod, "spawn_worker", fake_spawn([json.dumps({"type": "started", "url": "u"})])
    )
    jid = await job_manager.create_job("https://instagram.com/p/x/", "instagram")
    await job_manager.wait_for(jid)
    state = job_manager.get(jid)
    assert state is not None
    assert state.status is JobStatus.FAILED
    assert state.final_summary["reason"] == "worker-crash"


async def test_non_json_worker_line_is_ignored(
    job_manager, cookie_store, fake_spawn, monkeypatch
) -> None:
    cookie_store.update(ig_sessionid="SID")
    monkeypatch.setattr(
        mgr_mod,
        "spawn_worker",
        fake_spawn(
            [
                "this is not json",
                json.dumps({"type": "completed", "exit_status": 0, "downloaded": 0, "skipped": 0}),
            ]
        ),
    )
    jid = await job_manager.create_job("https://instagram.com/p/x/", "instagram")
    await job_manager.wait_for(jid)
    state = job_manager.get(jid)
    assert state is not None
    assert state.status is JobStatus.COMPLETED


async def test_gc_removes_only_old_terminal_jobs(job_manager) -> None:
    old = JobState(
        id="old",
        url="u",
        platform="instagram",
        status=JobStatus.COMPLETED,
        ended_at=time.time() - 7200,
    )
    fresh = JobState(
        id="fresh",
        url="u",
        platform="instagram",
        status=JobStatus.COMPLETED,
        ended_at=time.time(),
    )
    running = JobState(id="run", url="u", platform="instagram", status=JobStatus.RUNNING)
    job_manager._jobs["old"] = old
    job_manager._jobs["fresh"] = fresh
    job_manager._jobs["run"] = running

    removed = await job_manager.gc()
    assert removed == 1
    assert "old" not in job_manager._jobs
    assert "fresh" in job_manager._jobs
    assert "run" in job_manager._jobs


async def test_list_jobs_sorted_newest_first(job_manager) -> None:
    await job_manager.create_job("https://instagram.com/p/a/", "instagram")
    await job_manager.create_job("https://instagram.com/p/b/", "instagram")
    jobs = job_manager.list_jobs()
    assert [j.url for j in jobs] == ["https://instagram.com/p/b/", "https://instagram.com/p/a/"]


# ------------------------------------------------------------------ failure annotation


def test_annotate_promotes_login_wall_to_login_required() -> None:
    from collections import deque

    event: dict = {"type": "failed", "reason": "dl-failed"}
    mgr_mod._annotate_failure(
        event,
        deque(["gallery_dl.exception.AuthRequired: You must be logged in to continue viewing."]),
    )
    assert event["reason"] == "login-required"
    assert "Settings" in event["message"]
    # Plain language for the operator, not the raw tail.
    assert "worker output" not in event["message"]


def test_rate_limit_beats_login_wall_in_annotation() -> None:
    """FB's block page carries login-ish wording; "wait it out" is the correct advice there."""
    from collections import deque

    event: dict = {"type": "failed", "reason": "dl-failed"}
    mgr_mod._annotate_failure(
        event,
        deque(
            [
                "AbortExtraction: You've been temporarily blocked from viewing images.",
                "AuthRequired: You must be logged in to continue viewing images.",
            ]
        ),
    )
    assert event["reason"] == "rate-limited"


def test_annotate_keeps_reason_and_appends_tail_for_ordinary_failures() -> None:
    from collections import deque

    event: dict = {"type": "failed", "reason": "dl-failed", "message": "boom"}
    mgr_mod._annotate_failure(event, deque(["error:facebook:HttpError: '404 Not Found'"]))
    assert event["reason"] == "dl-failed"
    assert "404 Not Found" in event["message"]
