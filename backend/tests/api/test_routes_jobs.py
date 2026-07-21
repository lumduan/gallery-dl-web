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
