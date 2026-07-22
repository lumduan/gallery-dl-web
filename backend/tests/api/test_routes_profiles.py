from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI
from PIL import Image

from gallery_dl_web.config import Settings


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _seed(tmp_settings: Settings, platform: str, name: str, files: list[str]) -> Path:
    pdir = tmp_settings.downloads_dir / platform / name
    pdir.mkdir(parents=True, exist_ok=True)
    for fn in files:
        Image.new("RGB", (40, 40), "red").save(pdir / fn)
    return pdir


async def test_list_and_get(app: FastAPI, tmp_settings: Settings) -> None:
    _seed(tmp_settings, "facebook", "susan", ["pic.jpg"])
    await app.state.profile_store.reconcile("facebook", "susan")
    async with await _client(app) as c:
        r = await c.get("/api/profiles")
        assert r.status_code == 200
        assert "susan" in [p["name"] for p in r.json()]
        r2 = await c.get("/api/profiles/facebook/susan")
        assert r2.status_code == 200
        body = r2.json()
        assert body["image_count"] == 1
        assert body["images"][0]["thumb_url"].endswith("/thumb/pic.jpg")
        assert body["avatar_url"] is not None


async def test_file_and_thumbnail(app: FastAPI, tmp_settings: Settings) -> None:
    _seed(tmp_settings, "facebook", "susan", ["pic.jpg"])
    await app.state.profile_store.reconcile("facebook", "susan")
    async with await _client(app) as c:
        rf = await c.get("/api/profiles/facebook/susan/file/pic.jpg")
        assert rf.status_code == 200
        assert rf.content[:3] == b"\xff\xd8\xff"
        rt = await c.get("/api/profiles/facebook/susan/thumb/pic.jpg")
        assert rt.status_code == 200
        assert rt.headers["content-type"] == "image/jpeg"
        rne = await c.get("/api/profiles/facebook/susan/thumb/foo.mp4")
        assert rne.status_code == 404


async def test_zip_then_delete(app: FastAPI, tmp_settings: Settings) -> None:
    _seed(tmp_settings, "instagram", "mike", ["a.jpg", "b.jpg"])
    await app.state.profile_store.reconcile("instagram", "mike")
    async with await _client(app) as c:
        rz = await c.get("/api/profiles/instagram/mike/zip")
        assert rz.status_code == 200
        assert rz.headers["content-type"] == "application/zip"
        assert (tmp_settings.data_dir / "zips" / "instagram_mike.zip").exists()
        rd = await c.delete("/api/profiles/instagram/mike")
        assert rd.status_code == 204
    assert not (tmp_settings.downloads_dir / "instagram" / "mike").exists()
    assert not (tmp_settings.data_dir / "zips" / "instagram_mike.zip").exists()


async def test_invalid_platform_404(app: FastAPI) -> None:
    async with await _client(app) as c:
        assert (await c.get("/api/profiles/twitter/someone")).status_code == 404


async def test_missing_profile_404(app: FastAPI) -> None:
    async with await _client(app) as c:
        assert (await c.get("/api/profiles/facebook/nobody")).status_code == 404


async def test_traversal_rejected(app: FastAPI, tmp_settings: Settings) -> None:
    _seed(tmp_settings, "facebook", "susan", ["pic.jpg"])
    await app.state.profile_store.reconcile("facebook", "susan")
    async with await _client(app) as c:
        r = await c.get("/api/profiles/facebook/susan/file/../../../etc/passwd")
    assert r.status_code in (400, 404)


async def test_spaced_display_name_urls_are_encoded(app: FastAPI, tmp_settings: Settings) -> None:
    # gallery-dl uses the profile's display name (with spaces) as the {username} folder.
    _seed(tmp_settings, "facebook", "Jane Q Public", ["pic.jpg"])
    await app.state.profile_store.reconcile("facebook", "Jane Q Public")
    async with await _client(app) as c:
        r = await c.get("/api/profiles/facebook/Jane%20Q%20Public")
        assert r.status_code == 200
        thumb_url = r.json()["images"][0]["thumb_url"]
        assert "%20" in thumb_url and " " not in thumb_url
        rt = await c.get(thumb_url)
        assert rt.status_code == 200
