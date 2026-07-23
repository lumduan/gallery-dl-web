from __future__ import annotations

import asyncio
import json
import zipfile

import httpx
from fastapi import FastAPI

from gallery_dl_web.api.routes_jobs import detect_platform
from gallery_dl_web.config import Settings
from gallery_dl_web.jobs import manager as mgr_mod


def test_detect_platform() -> None:
    assert detect_platform("https://www.instagram.com/p/x/") == "instagram"
    assert detect_platform("https://facebook.com/photo/1") == "facebook"
    assert detect_platform("https://fb.watch/abc/") == "facebook"
    assert detect_platform("https://example.com/") is None


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_create_unknown_url_400(app: FastAPI) -> None:
    async with await _client(app) as client:
        r = await client.post("/api/jobs", json={"url": "https://example.com/"})
    assert r.status_code == 400


async def test_create_ig_queued(app: FastAPI) -> None:
    async with await _client(app) as client:
        r = await client.post("/api/jobs", json={"url": "https://instagram.com/p/x/"})
    assert r.status_code == 202
    data = r.json()
    assert data["status"] == "queued"
    assert "job_id" in data


async def test_get_job_404(app: FastAPI) -> None:
    async with await _client(app) as client:
        r = await client.get("/api/jobs/nope")
    assert r.status_code == 404


async def test_events_404(app: FastAPI) -> None:
    async with await _client(app) as client:
        r = await client.get("/api/jobs/nope/events")
    assert r.status_code == 404


async def test_list_jobs(app: FastAPI) -> None:
    async with await _client(app) as client:
        await client.post("/api/jobs", json={"url": "https://instagram.com/p/x/"})
        r = await client.get("/api/jobs")
    assert r.status_code == 200
    assert len(r.json()) >= 1


async def test_sse_replays_terminal_history(
    app: FastAPI, cookie_store, fake_spawn, monkeypatch
) -> None:
    cookie_store.update(ig_sessionid="SID")
    mgr = app.state.job_manager
    monkeypatch.setattr(
        mgr_mod,
        "spawn_worker",
        fake_spawn(
            [
                json.dumps({"type": "started", "url": "u"}),
                json.dumps({"type": "completed", "exit_status": 0, "downloaded": 1, "skipped": 0}),
            ]
        ),
    )
    jid = await mgr.create_job("https://instagram.com/p/x/", "instagram")
    await mgr.wait_for(jid)

    text = ""
    async with (
        await _client(app) as client,
        client.stream("GET", f"/api/jobs/{jid}/events") as resp,
    ):
        assert resp.status_code == 200
        async for chunk in resp.aiter_text():
            text += chunk
    assert '"type": "completed"' in text
    assert "event: end" in text


async def test_sse_live_stream(app: FastAPI, cookie_store, fake_spawn, monkeypatch) -> None:
    cookie_store.update(ig_sessionid="SID")
    # A small per-line delay so SSE subscribes before the job terminates (exercises the live path).
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
                json.dumps({"type": "completed", "exit_status": 0, "downloaded": 1, "skipped": 0}),
            ],
            delay=0.05,
        ),
    )
    mgr = app.state.job_manager
    jid = await mgr.create_job("https://instagram.com/p/x/", "instagram")

    text = ""
    async with (
        await _client(app) as client,
        client.stream("GET", f"/api/jobs/{jid}/events") as resp,
    ):
        async for chunk in resp.aiter_text():
            text += chunk
            if "event: end" in text:
                break
    await mgr.wait_for(jid)
    assert '"type": "completed"' in text


async def test_zip_endpoint(
    app: FastAPI, cookie_store, fake_spawn, monkeypatch, tmp_settings: Settings
) -> None:
    downloads = tmp_settings.downloads_dir
    (downloads / "instagram").mkdir(parents=True)
    pic = downloads / "instagram" / "a.jpg"
    pic.write_bytes(b"image-bytes")

    cookie_store.update(ig_sessionid="SID")
    mgr = app.state.job_manager
    monkeypatch.setattr(
        mgr_mod,
        "spawn_worker",
        fake_spawn(
            [
                json.dumps({"type": "started", "url": "u"}),
                json.dumps(
                    {"type": "file", "event": "downloaded", "path": str(pic), "filename": "a.jpg"}
                ),
                json.dumps({"type": "completed", "exit_status": 0, "downloaded": 1, "skipped": 0}),
            ]
        ),
    )
    jid = await mgr.create_job("https://instagram.com/p/x/", "instagram")
    await mgr.wait_for(jid)

    async with await _client(app) as client:
        r = await client.get(f"/api/jobs/{jid}/zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(__import__("io").BytesIO(r.content)) as zf:
        names = zf.namelist()
    assert "instagram/a.jpg" in names


async def test_zip_no_files_404(app: FastAPI, cookie_store, fake_spawn, monkeypatch) -> None:
    cookie_store.update(ig_sessionid="SID")
    mgr = app.state.job_manager
    monkeypatch.setattr(
        mgr_mod,
        "spawn_worker",
        fake_spawn(
            [json.dumps({"type": "completed", "exit_status": 0, "downloaded": 0, "skipped": 0})]
        ),
    )
    jid = await mgr.create_job("https://instagram.com/p/x/", "instagram")
    await mgr.wait_for(jid)
    async with await _client(app) as client:
        r = await client.get(f"/api/jobs/{jid}/zip")
    assert r.status_code == 404


async def test_missing_cookies_via_http(app: FastAPI) -> None:
    """No cookies set -> job reaches a failed terminal state."""
    mgr = app.state.job_manager
    jid = await mgr.create_job("https://instagram.com/p/x/", "instagram")
    await mgr.wait_for(jid)
    async with await _client(app) as client:
        r = await client.get(f"/api/jobs/{jid}")
    body = r.json()
    assert body["status"] == "failed"
    assert body["final_summary"]["reason"] == "missing-cookies"
    # Avoid an unawaited-task warning in the loop teardown.
    await asyncio.sleep(0)


# ------------------------------------------------------------------ pause / resume / cancel


_SLOW = [
    json.dumps({"type": "started", "url": "u"}),
    json.dumps({"type": "heartbeat", "beat": 1, "elapsed": 1}),
    json.dumps({"type": "completed", "exit_status": 0}),
]


async def _long_running_job(app: FastAPI, cookie_store, fake_spawn, monkeypatch) -> str:
    cookie_store.update(ig_sessionid="SID")
    app.state.settings.stall_liveness_seconds = 30.0
    app.state.settings.stall_warmup_seconds = 30.0
    app.state.settings.stall_kill_grace_seconds = 0.05
    monkeypatch.setattr(mgr_mod, "spawn_worker", fake_spawn(_SLOW, delay=0.3))
    mgr = app.state.job_manager
    jid = await mgr.create_job("https://instagram.com/someone/", "instagram")
    for _ in range(200):  # let the worker spawn so pause has something to signal
        if mgr.get(jid).started_at is not None:
            break
        await asyncio.sleep(0.01)
    return str(jid)


async def test_pause_resume_cancel_round_trip(
    app: FastAPI, cookie_store, fake_spawn, monkeypatch
) -> None:
    jid = await _long_running_job(app, cookie_store, fake_spawn, monkeypatch)
    async with await _client(app) as client:
        r = await client.post(f"/api/jobs/{jid}/pause")
        assert r.status_code == 200
        assert r.json()["status"] == "paused"
        assert r.json()["paused_at"] is not None

        # Pausing twice is a conflict, not a silent no-op.
        assert (await client.post(f"/api/jobs/{jid}/pause")).status_code == 409

        r = await client.post(f"/api/jobs/{jid}/resume")
        assert r.status_code == 200
        assert r.json()["status"] in {"queued", "running"}

        # Resuming a job that is not paused is likewise a conflict.
        assert (await client.post(f"/api/jobs/{jid}/resume")).status_code == 409

        r = await client.post(f"/api/jobs/{jid}/cancel")
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"
        # Terminal: every further control action conflicts.
        assert (await client.post(f"/api/jobs/{jid}/cancel")).status_code == 409
        assert (await client.post(f"/api/jobs/{jid}/pause")).status_code == 409


async def test_control_endpoints_404_on_unknown_job(app: FastAPI) -> None:
    async with await _client(app) as client:
        for action in ("pause", "resume", "cancel"):
            r = await client.post(f"/api/jobs/nope/{action}")
            assert r.status_code == 404, action


async def test_list_jobs_active_filter(app: FastAPI, cookie_store, fake_spawn, monkeypatch) -> None:
    """The queue page polls ?active=1; a finished job must drop out of it."""
    jid = await _long_running_job(app, cookie_store, fake_spawn, monkeypatch)
    async with await _client(app) as client:
        active = (await client.get("/api/jobs", params={"active": 1})).json()
        assert [j["id"] for j in active] == [jid]
        # Live counters and the profile name travel with the summary — no SSE needed per row.
        assert active[0]["profile"] == "someone"
        assert "downloaded" in active[0] and "skipped" in active[0]

        await client.post(f"/api/jobs/{jid}/cancel")
        assert (await client.get("/api/jobs", params={"active": 1})).json() == []
        assert [j["id"] for j in (await client.get("/api/jobs")).json()] == [jid]
