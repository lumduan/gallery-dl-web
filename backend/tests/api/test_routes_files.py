from __future__ import annotations

import httpx
from fastapi import FastAPI

from gallery_dl_web.config import Settings


async def test_list_empty(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.get("/api/files")
    assert r.status_code == 200
    assert r.json() == {"files": []}


async def test_list_and_download(app: FastAPI, tmp_settings: Settings) -> None:
    downloads = tmp_settings.downloads_dir
    (downloads / "instagram").mkdir(parents=True)
    (downloads / "instagram" / "pic.jpg").write_bytes(b"hello-image")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.get("/api/files")
        assert r.status_code == 200
        files = r.json()["files"]
        assert len(files) == 1
        assert files[0]["name"] == "pic.jpg"
        assert files[0]["platform"] == "instagram"
        assert files[0]["size"] == len(b"hello-image")

        r2 = await client.get("/api/files/download", params={"path": "instagram/pic.jpg"})
    assert r2.status_code == 200
    assert r2.content == b"hello-image"


async def test_path_traversal_rejected(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.get("/api/files/download", params={"path": "../../../etc/passwd"})
    assert r.status_code in (400, 404)


async def test_download_missing_404(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.get("/api/files/download", params={"path": "nope.jpg"})
    assert r.status_code == 404


async def test_archive_files_hidden(app: FastAPI, tmp_settings: Settings) -> None:
    archive_dir = tmp_settings.data_dir / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "instagram.sqlite").write_bytes(b"\x00")
    downloads = tmp_settings.downloads_dir
    downloads.mkdir(parents=True)
    (downloads / "pic.jpg").write_bytes(b"x")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.get("/api/files")
    names = {f["name"] for f in r.json()["files"]}
    assert "pic.jpg" in names
    assert "instagram.sqlite" not in names
