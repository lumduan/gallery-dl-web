"""Stall-detection + adaptive retry tests."""

from __future__ import annotations

import json

from gallery_dl_web.config import Settings
from gallery_dl_web.jobs import manager as mgr_mod
from gallery_dl_web.jobs.models import JobState, JobStatus


def _ev(d: dict) -> str:
    return json.dumps(d)


async def test_stall_retries_then_fails(
    job_manager, cookie_store, fake_spawn, monkeypatch, tmp_settings: Settings
) -> None:
    # Tiny threshold + 1 retry; the fake worker hangs (readline sleeps past the deadline).
    tmp_settings.stall_floor_seconds = 0.05
    tmp_settings.stall_max_retries = 1
    tmp_settings.stall_kill_grace_seconds = 0.05
    cookie_store.update(ig_sessionid="SID")
    monkeypatch.setattr(mgr_mod, "spawn_worker", fake_spawn([], delay=0.5))

    jid = await job_manager.create_job("https://instagram.com/p/x/", "instagram")
    await job_manager.wait_for(jid)
    state = job_manager.get(jid)
    assert state is not None
    types = [e["type"] for e in state.events]

    assert state.status is JobStatus.FAILED
    assert "stalled" in types
    assert "retrying" in types
    assert types[-1] == "failed"
    assert state.final_summary is not None
    assert state.final_summary["reason"] == "stalled"


async def test_no_stall_on_normal_completion(
    job_manager, cookie_store, fake_spawn, monkeypatch
) -> None:
    cookie_store.update(ig_sessionid="SID")
    lines = [
        _ev({"type": "started", "url": "u"}),
        _ev({"type": "file", "event": "downloaded", "path": "/o/a.jpg", "filename": "a.jpg"}),
        _ev({"type": "completed", "exit_status": 0, "downloaded": 1, "skipped": 0}),
    ]
    monkeypatch.setattr(mgr_mod, "spawn_worker", fake_spawn(lines))

    jid = await job_manager.create_job("https://instagram.com/p/x/", "instagram")
    await job_manager.wait_for(jid)
    state = job_manager.get(jid)
    assert state is not None
    assert state.status is JobStatus.COMPLETED
    assert "stalled" not in [e["type"] for e in state.events]


async def test_worker_progress_replaced_by_job_level(
    job_manager, cookie_store, fake_spawn, monkeypatch
) -> None:
    cookie_store.update(ig_sessionid="SID")
    # Worker's own progress (99,99) is dropped; manager emits job-level counts.
    lines = [
        _ev({"type": "started"}),
        _ev({"type": "file", "event": "downloaded", "path": "/o/a.jpg", "filename": "a.jpg"}),
        _ev({"type": "progress", "downloaded": 99, "skipped": 99}),
        _ev({"type": "completed", "exit_status": 0}),
    ]
    monkeypatch.setattr(mgr_mod, "spawn_worker", fake_spawn(lines))

    jid = await job_manager.create_job("https://instagram.com/p/x/", "instagram")
    await job_manager.wait_for(jid)
    state = job_manager.get(jid)
    assert state is not None
    progresses = [e for e in state.events if e["type"] == "progress"]
    assert len(progresses) == 1  # only the manager's job-level progress
    assert progresses[0]["downloaded"] == 1  # not the worker's bogus 99


def test_stall_threshold_math(job_manager, tmp_settings: Settings) -> None:
    s = tmp_settings
    s.stall_floor_seconds = 90
    s.stall_multiplier = 4
    s.stall_cap_seconds = 600
    s.stall_backoff = 1.5
    st = JobState(id="j", url="u", platform="instagram")

    assert job_manager._stall_threshold(st, 0) == 90  # no files -> floor
    assert job_manager._stall_threshold(st, 1) == 135  # floor * backoff

    st.first_file_ts = 0.0
    st.last_file_ts = 30.0
    st.file_count = 4  # avg inter-file = 10s -> 4*10=40 < floor 90
    assert job_manager._stall_threshold(st, 0) == 90

    st.last_file_ts = 150.0  # avg = 50 -> 4*50=200 > floor
    assert job_manager._stall_threshold(st, 0) == 200

    st.last_file_ts = 3000.0  # avg large -> exceeds cap
    assert job_manager._stall_threshold(st, 0) == 600
