from __future__ import annotations

import os

import httpx
from fastapi import FastAPI

from gallery_dl_web.config import Settings


async def test_list_empty(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.get("/api/files")
    assert r.status_code == 200
    assert r.json() == {"files": [], "total": 0, "truncated": False}


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


# --- the response shape -------------------------------------------------------------------------
#
# The event-loop, single-flight and cache properties are tested in tests/files/test_index.py, at the
# level they actually live. Two earlier attempts to assert them THROUGH this route both passed with
# the offload deleted -- ordering through httpx is decided by its own await points, not by the
# blocking -- so they are deliberately not duplicated here.


async def test_the_listing_is_capped_and_reports_the_real_total(
    app: FastAPI, tmp_settings: Settings
) -> None:
    """`total` is what is on disk; `files` is a page of it. Serialising 427k entries was tens of
    megabytes of JSON for a page that shows a screenful."""
    d = tmp_settings.downloads_dir / "instagram"
    d.mkdir(parents=True)
    for i in range(12):
        (d / f"{i:03}.jpg").write_bytes(b"x")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.get("/api/files", params={"limit": 5})
        over = await client.get("/api/files", params={"limit": 99999})

    body = r.json()
    assert len(body["files"]) == 5
    assert body["total"] == 12
    assert body["truncated"] is True
    assert over.status_code == 422, "limit must be bounded, not merely large"


async def test_the_listing_is_newest_first(app: FastAPI, tmp_settings: Settings) -> None:
    """The cap only means anything if the page is the *newest* slice."""
    d = tmp_settings.downloads_dir / "instagram"
    d.mkdir(parents=True)
    for i in range(5):
        f = d / f"{i}.jpg"
        f.write_bytes(b"x")
        os.utime(f, (1_000_000 + i * 60, 1_000_000 + i * 60))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.get("/api/files", params={"limit": 2})

    assert [e["name"] for e in r.json()["files"]] == ["4.jpg", "3.jpg"]
