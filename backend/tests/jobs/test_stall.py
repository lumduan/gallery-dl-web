"""Stall-detection + adaptive retry tests."""

from __future__ import annotations

import json

from gallery_dl_web.config import Settings
from gallery_dl_web.jobs import manager as mgr_mod
from gallery_dl_web.jobs.models import JobState, JobStatus


def _ev(d: dict) -> str:
    return json.dumps(d)


async def test_warmup_stall_retries_then_fails(
    job_manager, cookie_store, fake_spawn, monkeypatch, tmp_settings: Settings
) -> None:
    # Tiny warm-up budget + 1 retry; the fake worker hangs (readline sleeps past the deadline).
    # No file ever arrives, so this is the WARM-UP path -> reason "no-progress".
    tmp_settings.stall_warmup_seconds = 0.05
    tmp_settings.stall_warmup_max_retries = 1
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
    assert state.final_summary["reason"] == "no-progress"
    # The warm-up phase is named so the UI can explain what actually timed out.
    stalled = next(e for e in state.events if e["type"] == "stalled")
    assert stalled["phase"] == "warmup"


async def test_warmup_budget_is_not_the_steady_state_floor(
    job_manager, cookie_store, fake_spawn, monkeypatch, tmp_settings: Settings
) -> None:
    """The regression: a slow-to-start extraction must NOT be killed by the 90s steady-state floor.

    The worker emits nothing for longer than stall_floor_seconds, then delivers a file and
    completes — exactly what Instagram/Facebook profile enumeration looks like.
    """
    tmp_settings.stall_floor_seconds = 0.1  # steady-state floor: shorter than the warm-up silence
    tmp_settings.stall_warmup_seconds = 5.0  # warm-up budget: generous
    tmp_settings.stall_liveness_seconds = 5.0
    cookie_store.update(ig_sessionid="SID")
    lines = [
        _ev({"type": "started", "url": "u"}),
        _ev({"type": "file", "event": "downloaded", "path": "/o/a.jpg", "filename": "a.jpg"}),
        _ev({"type": "completed", "exit_status": 0}),
    ]
    # Long silence before the first file (0.4s > stall_floor 0.1s), then steady delivery.
    # Under the old single-deadline logic the 0.4s gap killed the worker before it downloaded.
    monkeypatch.setattr(mgr_mod, "spawn_worker", fake_spawn(lines, delays=[0.01, 0.4, 0.01, 0.01]))

    jid = await job_manager.create_job("https://instagram.com/x/", "instagram")
    await job_manager.wait_for(jid)
    state = job_manager.get(jid)
    assert state is not None
    assert state.status is JobStatus.COMPLETED
    assert "stalled" not in [e["type"] for e in state.events]


async def test_prepare_events_count_as_progress(
    job_manager, cookie_store, fake_spawn, monkeypatch, tmp_settings: Settings
) -> None:
    """Observed live: 90 `prepare`s in 60s with no `file` event, and the job was killed anyway.

    gallery-dl resolves items that never produce a file event (filtered, or a phase with nothing
    downloadable). Those prepares are proof of work and must reset the progress clock.
    """
    tmp_settings.stall_warmup_seconds = 5.0
    tmp_settings.stall_floor_seconds = 0.25  # shorter than the total prepare run below
    tmp_settings.stall_liveness_seconds = 5.0
    cookie_store.update(ig_sessionid="SID")
    lines = [
        _ev({"type": "started", "url": "u"}),
        _ev({"type": "file", "event": "downloaded", "path": "/o/a.jpg", "filename": "a.jpg"}),
        # A long run of prepares with no file event — 6 * 0.1s = 0.6s > stall_floor 0.25s.
        *[_ev({"type": "prepare", "filename": f"{i}.jpg"}) for i in range(6)],
        _ev({"type": "completed", "exit_status": 0}),
    ]
    monkeypatch.setattr(mgr_mod, "spawn_worker", fake_spawn(lines, delay=0.1))

    jid = await job_manager.create_job("https://instagram.com/x/", "instagram")
    await job_manager.wait_for(jid)
    state = job_manager.get(jid)
    assert state is not None
    assert state.status is JobStatus.COMPLETED
    assert "stalled" not in [e["type"] for e in state.events]


async def test_heartbeat_keeps_job_alive_but_does_not_reset_file_clock(
    job_manager, cookie_store, fake_spawn, monkeypatch, tmp_settings: Settings
) -> None:
    """Heartbeats prove liveness; they must not be mistaken for download progress."""
    tmp_settings.stall_warmup_seconds = 5.0
    tmp_settings.stall_liveness_seconds = 5.0
    cookie_store.update(ig_sessionid="SID")
    lines = [
        _ev({"type": "started", "url": "u"}),
        _ev({"type": "heartbeat", "beat": 1}),
        _ev({"type": "heartbeat", "beat": 2}),
        _ev({"type": "file", "event": "downloaded", "path": "/o/a.jpg", "filename": "a.jpg"}),
        _ev({"type": "completed", "exit_status": 0}),
    ]
    monkeypatch.setattr(mgr_mod, "spawn_worker", fake_spawn(lines, delay=0.05))

    jid = await job_manager.create_job("https://instagram.com/x/", "instagram")
    await job_manager.wait_for(jid)
    state = job_manager.get(jid)
    assert state is not None
    assert state.status is JobStatus.COMPLETED
    types = [e["type"] for e in state.events]
    assert types.count("heartbeat") == 2  # forwarded to the UI
    assert state.file_count == 1  # heartbeats are not files


async def test_stderr_is_drained_and_surfaced_on_failure(
    job_manager, cookie_store, fake_spawn, monkeypatch, tmp_settings: Settings
) -> None:
    """A chatty worker must not wedge the job, and its output must reach the failure message."""
    tmp_settings.stall_warmup_seconds = 0.05
    tmp_settings.stall_warmup_max_retries = 0
    tmp_settings.stall_kill_grace_seconds = 0.05
    cookie_store.update(ig_sessionid="SID")
    # Far more than a 64 KB pipe would hold, which is what blocks a real worker mid-write.
    noisy = [f"error: line {i} something went wrong on the remote side\n" for i in range(2000)]
    monkeypatch.setattr(mgr_mod, "spawn_worker", fake_spawn([], delay=0.5, stderr_lines=noisy))

    jid = await job_manager.create_job("https://instagram.com/x/", "instagram")
    await job_manager.wait_for(jid)
    state = job_manager.get(jid)
    assert state is not None
    assert state.status is JobStatus.FAILED
    assert state.final_summary is not None
    assert "worker output:" in state.final_summary["message"]
    assert "something went wrong" in state.final_summary["message"]


async def test_unwritable_downloads_dir_fails_fast(
    job_manager, cookie_store, fake_spawn, monkeypatch, tmp_settings: Settings
) -> None:
    """A misconfigured DOWNLOADS_DIR must fail immediately, not stall for minutes."""
    cookie_store.update(ig_sessionid="SID")
    tmp_settings.downloads_dir = tmp_settings.downloads_dir / "nope"
    monkeypatch.setattr(mgr_mod.os, "access", lambda *a, **k: False)

    spawned = False

    def _boom(*_a: object, **_k: object) -> None:
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(mgr_mod, "spawn_worker", _boom)

    jid = await job_manager.create_job("https://instagram.com/x/", "instagram")
    await job_manager.wait_for(jid)
    state = job_manager.get(jid)
    assert state is not None
    assert state.status is JobStatus.FAILED
    assert state.final_summary is not None
    assert state.final_summary["reason"] == "downloads-dir-unwritable"
    assert not spawned  # never even tried to run gallery-dl


async def test_unwritable_platform_subdir_fails_fast(
    job_manager, cookie_store, monkeypatch, tmp_settings: Settings
) -> None:
    """Writable root + unwritable platform subdir — reads work, every download fails.

    Seen in the wild after copying media onto an NFS share as root: `root_squash` landed the
    tree as nobody:nogroup 0755, so archived files reported `skipped` while all 489 resolved
    downloads silently failed.
    """
    cookie_store.update(ig_sessionid="SID")
    tmp_settings.downloads_dir.mkdir(parents=True, exist_ok=True)
    (tmp_settings.downloads_dir / "instagram").mkdir(exist_ok=True)

    real_access = mgr_mod.os.access

    def fake_access(path, mode, **kw):
        return False if str(path).endswith("instagram") else real_access(path, mode, **kw)

    monkeypatch.setattr(mgr_mod.os, "access", fake_access)

    spawned = False

    def _boom(*_a: object, **_k: object) -> None:
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(mgr_mod, "spawn_worker", _boom)

    jid = await job_manager.create_job("https://instagram.com/x/", "instagram")
    await job_manager.wait_for(jid)
    state = job_manager.get(jid)
    assert state is not None
    assert state.status is JobStatus.FAILED
    assert state.final_summary is not None
    assert state.final_summary["reason"] == "downloads-dir-unwritable"
    assert "not writable" in state.final_summary["message"]
    assert not spawned


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
    s.stall_warmup_seconds = 600
    s.stall_floor_seconds = 90
    s.stall_multiplier = 4
    s.stall_cap_seconds = 900
    s.stall_backoff = 1.5
    st = JobState(id="j", url="u", platform="instagram")

    # No activity at all yet -> WARM-UP budget, not the steady-state floor. This is the regression
    # guard: returning 90 here killed profiles whose enumeration legitimately takes minutes.
    assert job_manager._stall_threshold(st, 0) == 600
    assert job_manager._stall_threshold(st, 1) == 900  # 600*1.5, clamped by the cap

    # A `prepare` alone ends warm-up — gallery-dl is demonstrably working, even with no file yet.
    st.last_activity_ts = 10.0
    assert job_manager._stall_threshold(st, 0) == 90

    st.first_file_ts = 0.0
    st.last_file_ts = 30.0
    st.file_count = 4  # avg inter-file = 10s -> 4*10=40 < floor 90
    assert job_manager._stall_threshold(st, 0) == 90
    assert job_manager._stall_threshold(st, 1) == 135  # floor * backoff

    st.last_file_ts = 150.0  # avg = 50 -> 4*50=200 > floor
    assert job_manager._stall_threshold(st, 0) == 200

    st.last_file_ts = 3000.0  # avg large -> exceeds cap
    assert job_manager._stall_threshold(st, 0) == 900


def test_warmup_is_generous_by_default(tmp_settings: Settings) -> None:
    """Defaults must tolerate Instagram's 6-12s sleep-request during enumeration."""
    s = Settings(data_dir=tmp_settings.data_dir)
    assert s.stall_warmup_seconds >= 300
    assert s.stall_warmup_seconds > s.stall_floor_seconds
    assert s.heartbeat_seconds < s.stall_liveness_seconds  # or liveness trips on a healthy worker
