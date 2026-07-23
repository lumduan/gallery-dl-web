"""Operator control: pause (SIGSTOP + slot release), resume (SIGCONT), stop (terminal cancelled).

The behaviour these lock down came straight from a live incident: two large profiles held both
concurrency slots for hours, two more sat at ``queued`` with no explanation, and there was no way
to stand any of them down.
"""

from __future__ import annotations

import asyncio
import json
import signal
from typing import Any

import pytest

from gallery_dl_web.config import Settings
from gallery_dl_web.cookies.store import CookieStore
from gallery_dl_web.jobs import manager as mgr_mod
from gallery_dl_web.jobs.manager import JobControlError, JobManager
from gallery_dl_web.jobs.models import JobStatus
from tests.conftest import FakeProc


def _ev(d: dict) -> str:
    return json.dumps(d)


def _types(state) -> list[str]:
    return [e["type"] for e in state.events]


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    """Poll until predicate() is true; fail the test on timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached in time")


# A worker that keeps emitting heartbeats and never finishes on its own, so the test controls it.
_SLOW_LINES = [
    _ev({"type": "started", "url": "u"}),
    _ev({"type": "file", "event": "downloaded", "path": "/o/a.jpg", "filename": "a.jpg"}),
    _ev({"type": "heartbeat", "beat": 1, "elapsed": 1}),
    _ev({"type": "heartbeat", "beat": 2, "elapsed": 2}),
    _ev({"type": "completed", "exit_status": 0}),
]


@pytest.fixture
def paced_settings(tmp_settings: Settings) -> Settings:
    """Deadlines long enough that nothing trips while a test is fiddling with a job."""
    tmp_settings.stall_warmup_seconds = 30.0
    tmp_settings.stall_floor_seconds = 30.0
    tmp_settings.stall_liveness_seconds = 30.0
    tmp_settings.stall_kill_grace_seconds = 0.05
    return tmp_settings


@pytest.fixture
def spawns(monkeypatch) -> list[FakeProc]:
    """Patch spawn_worker with a slow fake worker and collect every process it hands out.

    Returned list doubles as the spawn log — ``len(spawns)`` is how "did resume respawn?" and
    "did a stopped job get retried?" are asserted.
    """
    procs: list[FakeProc] = []

    async def _spawn(python: str, payload: dict[str, Any]) -> FakeProc:  # noqa: ARG001
        proc = FakeProc(_SLOW_LINES, delay=0.3)
        proc.job_id = payload["job_id"]  # type: ignore[attr-defined]
        procs.append(proc)
        return proc

    monkeypatch.setattr(mgr_mod, "spawn_worker", _spawn)
    return procs


def _solo_manager(settings: Settings) -> JobManager:
    """A one-slot manager, so 'the queue is blocked' can be reproduced deterministically."""
    settings.max_concurrent_jobs = 1
    settings.stall_warmup_seconds = 30.0
    settings.stall_liveness_seconds = 30.0
    settings.stall_kill_grace_seconds = 0.05
    store = CookieStore(settings.cookies_path)
    store.update(ig_sessionid="SID")
    return JobManager(settings, store)


# ---------------------------------------------------------------------------- pause / resume


async def test_pause_sigstops_the_worker_and_flips_status(
    job_manager, cookie_store, spawns, paced_settings
) -> None:
    cookie_store.update(ig_sessionid="SID")
    jid = await job_manager.create_job("https://instagram.com/someone/", "instagram")
    await _wait_until(lambda: spawns)

    state = await job_manager.pause(jid)
    assert state.status is JobStatus.PAUSED
    assert signal.SIGSTOP in spawns[0].signals
    assert "paused" in _types(state)
    # The slot goes back once the read loop parks (one event-loop turn later, not in pause()).
    await _wait_until(lambda: state.holds_slot is False)

    await job_manager.cancel(jid)  # don't leave a paused job behind


async def test_pause_frees_the_slot_so_a_queued_job_starts(spawns, tmp_settings: Settings) -> None:
    """The user's actual complaint: a long download blocks every other profile.

    With one slot, job B must sit at ``queued`` until A is paused — then start on its own.
    """
    manager = _solo_manager(tmp_settings)

    a = await manager.create_job("https://instagram.com/a/", "instagram")
    b = await manager.create_job("https://instagram.com/b/", "instagram")
    await _wait_until(lambda: len(spawns) == 1)

    # B is stuck behind A's single slot — the exact state that reads as "broken" in the UI.
    assert manager.get(b).status is JobStatus.QUEUED

    await manager.pause(a)
    await _wait_until(lambda: len(spawns) == 2)
    assert manager.get(b).status is JobStatus.RUNNING
    assert [p.job_id for p in spawns] == [a, b]

    await manager.cancel(a)
    await manager.cancel(b)


async def test_resume_sigconts_and_does_not_re_enumerate(
    job_manager, cookie_store, spawns, paced_settings
) -> None:
    """Resume continues the SAME worker — no second spawn, so no re-walk of the profile."""
    cookie_store.update(ig_sessionid="SID")
    procs = spawns

    jid = await job_manager.create_job("https://instagram.com/someone/", "instagram")
    await _wait_until(lambda: procs)
    await job_manager.pause(jid)
    await job_manager.resume(jid)

    state = job_manager.get(jid)
    await _wait_until(lambda: "resumed" in _types(state))
    assert signal.SIGCONT in procs[0].signals
    assert len(procs) == 1, "resume must reuse the suspended worker, not spawn a fresh one"
    assert state.status is JobStatus.RUNNING
    assert state.paused_at is None

    await job_manager.cancel(jid)


async def test_paused_time_does_not_count_towards_the_stall_deadline(
    job_manager, cookie_store, monkeypatch, tmp_settings: Settings
) -> None:
    """A pause longer than the progress deadline must not be reported as a stall.

    Without the clock rebase on resume, the paused wall-time lands on the progress clock and the
    job is killed and retried the moment it comes back.
    """
    tmp_settings.stall_floor_seconds = 0.3  # shorter than the pause below
    tmp_settings.stall_warmup_seconds = 30.0
    tmp_settings.stall_liveness_seconds = 30.0
    tmp_settings.stall_kill_grace_seconds = 0.05
    cookie_store.update(ig_sessionid="SID")

    lines = [
        _ev({"type": "started", "url": "u"}),
        _ev({"type": "file", "event": "downloaded", "path": "/o/a.jpg", "filename": "a.jpg"}),
        _ev({"type": "completed", "exit_status": 0}),
    ]

    async def _spawn(python: str, payload: dict[str, Any]) -> FakeProc:  # noqa: ARG001
        return FakeProc(lines, delays=[0.02, 0.02, 0.05])

    monkeypatch.setattr(mgr_mod, "spawn_worker", _spawn)

    jid = await job_manager.create_job("https://instagram.com/someone/", "instagram")
    state = job_manager.get(jid)
    await _wait_until(lambda: state.file_count > 0)
    await job_manager.pause(jid)
    await asyncio.sleep(0.6)  # twice the progress deadline
    await job_manager.resume(jid)
    await job_manager.wait_for(jid)

    assert "stalled" not in _types(state)
    assert "retrying" not in _types(state)
    assert state.status is JobStatus.COMPLETED


async def test_double_pause_and_resume_of_a_running_job_are_rejected(
    job_manager, cookie_store, spawns, paced_settings
) -> None:
    cookie_store.update(ig_sessionid="SID")
    assert spawns is not None
    jid = await job_manager.create_job("https://instagram.com/someone/", "instagram")

    with pytest.raises(JobControlError):
        await job_manager.resume(jid)  # not paused
    await job_manager.pause(jid)
    with pytest.raises(JobControlError):
        await job_manager.pause(jid)  # already paused
    await job_manager.cancel(jid)


async def test_resume_before_the_read_loop_noticed_the_pause(
    job_manager, cookie_store, spawns, paced_settings
) -> None:
    """Pause then resume back-to-back, with no chance for the read loop to run in between.

    Two things went wrong here before ``_await_resume`` owned the slot handoff: the worker stayed
    SIGSTOPed forever because nothing was left to send SIGCONT, and the slot released by ``pause``
    was never re-acquired, so the job ran outside the concurrency limit.
    """
    cookie_store.update(ig_sessionid="SID")
    jid = await job_manager.create_job("https://instagram.com/someone/", "instagram")
    await _wait_until(lambda: spawns)

    await job_manager.pause(jid)
    await job_manager.resume(jid)  # same event-loop turn — the loop never saw the pause

    state = job_manager.get(jid)
    await _wait_until(lambda: state.status is JobStatus.RUNNING)
    assert signal.SIGCONT in spawns[0].signals, "worker must not be left suspended"
    assert state.holds_slot is True, "the job must still hold exactly one slot"
    assert state.paused_at is None
    await job_manager.cancel(jid)


# ---------------------------------------------------------------------------- stop


async def test_stop_emits_exactly_one_terminal_cancelled_and_no_retry(
    job_manager, cookie_store, spawns, paced_settings
) -> None:
    cookie_store.update(ig_sessionid="SID")
    procs = spawns

    jid = await job_manager.create_job("https://instagram.com/someone/", "instagram")
    await _wait_until(lambda: procs)
    await job_manager.cancel(jid)
    await job_manager.wait_for(jid)

    state = job_manager.get(jid)
    types = _types(state)
    assert state.status is JobStatus.CANCELLED
    assert types.count("cancelled") == 1
    assert [t for t in types if t in {"completed", "failed", "cancelled"}] == ["cancelled"]
    assert "retrying" not in types, "a stopped job must never be respawned"
    assert len(procs) == 1
    assert state.final_summary["reason"] == "cancelled"


async def test_stop_sends_sigcont_before_sigterm(
    job_manager, cookie_store, spawns, paced_settings
) -> None:
    """A SIGSTOPed process never handles SIGTERM — and proc.wait() would hang forever."""
    cookie_store.update(ig_sessionid="SID")

    jid = await job_manager.create_job("https://instagram.com/someone/", "instagram")
    await _wait_until(lambda: spawns)
    await job_manager.pause(jid)
    await job_manager.cancel(jid)

    sent = spawns[0].signals
    assert signal.SIGSTOP in sent and signal.SIGCONT in sent and signal.SIGTERM in sent
    assert sent.index(signal.SIGCONT) < sent.index(signal.SIGTERM)


async def test_stop_on_a_queued_job_never_spawns_a_worker(spawns, tmp_settings: Settings) -> None:
    manager = _solo_manager(tmp_settings)

    a = await manager.create_job("https://instagram.com/a/", "instagram")
    b = await manager.create_job("https://instagram.com/b/", "instagram")
    await _wait_until(lambda: len(spawns) == 1)

    await manager.cancel(b)
    assert manager.get(b).status is JobStatus.CANCELLED
    assert b not in [p.job_id for p in spawns]

    await manager.cancel(a)


async def test_stop_is_rejected_once_terminal(
    job_manager, cookie_store, fake_spawn, monkeypatch, paced_settings
) -> None:
    cookie_store.update(ig_sessionid="SID")
    lines = [_ev({"type": "started", "url": "u"}), _ev({"type": "completed", "exit_status": 0})]
    monkeypatch.setattr(mgr_mod, "spawn_worker", fake_spawn(lines))

    jid = await job_manager.create_job("https://instagram.com/someone/", "instagram")
    await job_manager.wait_for(jid)
    for action in (job_manager.pause, job_manager.resume, job_manager.cancel):
        with pytest.raises(JobControlError):
            await action(jid)


async def test_control_of_an_unknown_job_raises_keyerror(job_manager) -> None:
    for action in (job_manager.pause, job_manager.resume, job_manager.cancel):
        with pytest.raises(KeyError):
            await action("nope")


# ---------------------------------------------------------------------------- leak guard


async def test_gc_auto_cancels_a_job_paused_too_long(
    job_manager, cookie_store, spawns, paced_settings
) -> None:
    """A paused job is not terminal, so GC would otherwise keep its suspended worker forever."""
    paced_settings.pause_max_seconds = 0.05
    cookie_store.update(ig_sessionid="SID")
    assert spawns is not None

    jid = await job_manager.create_job("https://instagram.com/someone/", "instagram")
    await job_manager.pause(jid)
    await asyncio.sleep(0.1)
    await job_manager.gc()

    state = job_manager.get(jid)
    assert state.status is JobStatus.CANCELLED
    assert "paused for over" in state.final_summary["message"]


async def test_gc_leaves_a_freshly_paused_job_alone(
    job_manager, cookie_store, spawns, paced_settings
) -> None:
    paced_settings.pause_max_seconds = 0.0  # disabled
    cookie_store.update(ig_sessionid="SID")
    assert spawns is not None

    jid = await job_manager.create_job("https://instagram.com/someone/", "instagram")
    await job_manager.pause(jid)
    await job_manager.gc()
    assert job_manager.get(jid).status is JobStatus.PAUSED
    await job_manager.cancel(jid)
