from __future__ import annotations

import asyncio
from pathlib import Path

from gallery_dl_web.config import Settings
from gallery_dl_web.profiles.store import ProfileStore


def _settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    return Settings(
        data_dir=data,
        downloads_dir=data / "downloads",
        cookies_path=data / "c.json",
        cors_origins=["http://x"],
        max_concurrent_jobs=1,
    )


def _make_profile(tmp_path: Path, platform: str, name: str, files: list[tuple[str, bytes]]) -> Path:
    d = tmp_path / "data" / "downloads" / platform / name
    d.mkdir(parents=True)
    for fn, content in files:
        (d / fn).write_bytes(content)
    return d


def test_reconcile_classifies_and_counts(tmp_path: Path) -> None:
    _make_profile(
        tmp_path,
        "facebook",
        "susan",
        [("a.jpg", b"xx"), ("b.png", b"yyy"), ("v.mp4", b"zzzz")],
    )
    store = ProfileStore(_settings(tmp_path))
    meta = asyncio.run(store.reconcile("facebook", "susan"))
    assert meta is not None
    assert meta["image_count"] == 2
    assert meta["video_count"] == 1
    assert meta["total_bytes"] == 2 + 3 + 4
    assert meta["avatar"] is not None


def test_avatar_hint_preferred(tmp_path: Path) -> None:
    _make_profile(tmp_path, "facebook", "susan", [("pic.jpg", b"x"), ("avatar.jpg", b"yy")])
    store = ProfileStore(_settings(tmp_path))
    meta = asyncio.run(store.reconcile("facebook", "susan"))
    assert meta is not None
    assert "avatar" in meta["avatar"]


def test_load_and_list_all(tmp_path: Path) -> None:
    _make_profile(tmp_path, "facebook", "susan", [("a.jpg", b"x")])
    _make_profile(tmp_path, "instagram", "mike", [("b.jpg", b"y")])
    store = ProfileStore(_settings(tmp_path))
    asyncio.run(store.reconcile("facebook", "susan"))
    all_profiles = asyncio.run(store.list_all())
    names = sorted(p["name"] for p in all_profiles)
    # mike has files but no metadata yet -> list_all rebuilds it
    assert "susan" in names and "mike" in names
    assert store.load("facebook", "susan") is not None


def test_delete_removes_files_archive_metadata(tmp_path: Path) -> None:
    _make_profile(tmp_path, "facebook", "susan", [("a.jpg", b"x")])
    archive = tmp_path / "data" / "archive" / "facebook"
    archive.mkdir(parents=True)
    (archive / "susan.sqlite").write_text("x")
    store = ProfileStore(_settings(tmp_path))
    asyncio.run(store.reconcile("facebook", "susan"))
    removed = asyncio.run(store.delete("facebook", "susan"))
    assert removed
    assert not (tmp_path / "data" / "downloads" / "facebook" / "susan").exists()
    assert not (archive / "susan.sqlite").exists()
    assert store.load("facebook", "susan") is None


def test_reconcile_missing_dir(tmp_path: Path) -> None:
    store = ProfileStore(_settings(tmp_path))
    assert asyncio.run(store.reconcile("facebook", "nobody")) is None


def test_empty_profile_not_listed(tmp_path: Path) -> None:
    _make_profile(tmp_path, "facebook", "empty", [])
    store = ProfileStore(_settings(tmp_path))
    asyncio.run(store.reconcile("facebook", "empty"))
    all_profiles = asyncio.run(store.list_all())
    assert all_profiles == []


def test_reconcile_stores_archive_path(tmp_path: Path) -> None:
    _make_profile(tmp_path, "facebook", "susan", [("a.jpg", b"x")])
    store = ProfileStore(_settings(tmp_path))
    meta = asyncio.run(
        store.reconcile("facebook", "susan", archive_path="/data/archive/facebook/url_user.sqlite")
    )
    assert meta is not None
    assert meta["archive_path"] == "/data/archive/facebook/url_user.sqlite"


def test_delete_removes_url_username_archive(tmp_path: Path) -> None:
    # Folder name "susan" differs from the URL-username archive key; delete must use metadata.
    _make_profile(tmp_path, "facebook", "susan", [("a.jpg", b"x")])
    ap = tmp_path / "data" / "archive" / "facebook" / "url_user.sqlite"
    ap.parent.mkdir(parents=True)
    ap.write_text("x")
    store = ProfileStore(_settings(tmp_path))
    asyncio.run(store.reconcile("facebook", "susan", archive_path=str(ap)))
    asyncio.run(store.delete("facebook", "susan"))
    assert not ap.exists()
