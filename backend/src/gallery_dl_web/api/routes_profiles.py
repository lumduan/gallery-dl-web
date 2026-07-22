"""Profile management routes: list, inspect, serve files/thumbnails, zip (with TTL), delete."""

from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from gallery_dl_web.api.deps import get_profile_store, get_settings
from gallery_dl_web.config import Settings
from gallery_dl_web.profiles.store import ProfileStore
from gallery_dl_web.profiles.thumbnails import get_or_generate, is_image
from gallery_dl_web.profiles.urls import is_safe_profile_name
from gallery_dl_web.schemas.profiles import (
    ProfileFileEntry,
    ProfileMetadata,
    ProfileSummary,
)

router = APIRouter(prefix="/profiles", tags=["profiles"])

_PLATFORMS = {"instagram", "facebook"}
_HIDDEN_SUFFIXES = (".sqlite", ".sqlite-journal", ".sqlite-wal", ".sqlite-shm", ".db")


def _require_platform(platform: str) -> str:
    if platform not in _PLATFORMS:
        raise HTTPException(status_code=404, detail="unknown platform")
    return platform


def _require_name(name: str) -> str:
    if not is_safe_profile_name(name):
        raise HTTPException(status_code=404, detail="unknown profile")
    return name


def _profile_dir(settings: Settings, platform: str, name: str) -> Path:
    return settings.downloads_dir / platform / name


def _resolve_within(base: Path, rel: str) -> Path:
    """Resolve rel under base, rejecting traversal (404 on violation)."""
    from gallery_dl_web.api.paths import resolve_within

    try:
        return resolve_within(base, rel)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc


def _entry(entry: dict[str, Any], platform: str, name: str) -> ProfileFileEntry:
    fn = entry["filename"]
    base = f"/api/profiles/{platform}/{quote(name)}"
    return ProfileFileEntry(
        filename=fn,
        path=entry["path"],
        bytes=entry["bytes"],
        mtime=entry["mtime"],
        kind=entry["kind"],
        media_id=entry.get("media_id"),
        thumb_url=f"{base}/thumb/{quote(fn)}",
        file_url=f"{base}/file/{quote(fn)}",
    )


def _to_metadata(meta: dict[str, Any], platform: str, name: str) -> ProfileMetadata:
    base = f"/api/profiles/{platform}/{quote(name)}"
    avatar_url: str | None = None
    if meta.get("avatar"):
        avatar_url = f"{base}/file/{quote(Path(meta['avatar']).name)}"
    return ProfileMetadata(
        platform=platform,
        name=name,
        avatar=meta.get("avatar"),
        avatar_url=avatar_url,
        images=[_entry(e, platform, name) for e in meta.get("images", [])],
        videos=[_entry(e, platform, name) for e in meta.get("videos", [])],
        image_count=meta.get("image_count", 0),
        video_count=meta.get("video_count", 0),
        total_bytes=meta.get("total_bytes", 0),
        last_updated=meta.get("last_updated", 0),
    )


def _to_summary(meta: dict[str, Any]) -> ProfileSummary:
    platform, name = meta["platform"], meta["name"]
    avatar_url: str | None = None
    if meta.get("avatar"):
        avatar_url = (
            f"/api/profiles/{platform}/{quote(name)}/file/{quote(Path(meta['avatar']).name)}"
        )
    return ProfileSummary(
        platform=platform,
        name=name,
        avatar_url=avatar_url,
        image_count=meta.get("image_count", 0),
        video_count=meta.get("video_count", 0),
        total_bytes=meta.get("total_bytes", 0),
        last_updated=meta.get("last_updated", 0),
    )


@router.get("", response_model=list[ProfileSummary])
async def list_profiles(store: ProfileStore = Depends(get_profile_store)) -> list[ProfileSummary]:
    return [_to_summary(m) for m in await store.list_all()]


@router.get("/{platform}/{name}", response_model=ProfileMetadata)
async def get_profile(
    platform: str,
    name: str,
    store: ProfileStore = Depends(get_profile_store),
    settings: Settings = Depends(get_settings),
) -> ProfileMetadata:
    platform = _require_platform(platform)
    name = _require_name(name)
    if not _profile_dir(settings, platform, name).is_dir():
        raise HTTPException(status_code=404, detail="profile not found")
    meta = await store.load_or_rebuild(platform, name)
    if not meta:
        raise HTTPException(status_code=404, detail="profile has no media")
    return _to_metadata(meta, platform, name)


@router.get("/{platform}/{name}/file/{filename:path}")
async def get_file(
    platform: str,
    name: str,
    filename: str,
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    platform = _require_platform(platform)
    name = _require_name(name)
    full = _resolve_within(_profile_dir(settings, platform, name), filename)
    if not full.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(full, filename=full.name)


@router.get("/{platform}/{name}/thumb/{filename:path}")
async def get_thumb(
    platform: str,
    name: str,
    filename: str,
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    platform = _require_platform(platform)
    name = _require_name(name)
    if not is_image(filename):
        raise HTTPException(status_code=404, detail="no thumbnail for non-image")
    src = _resolve_within(_profile_dir(settings, platform, name), filename)
    if not src.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    try:
        thumb = get_or_generate(settings, platform, name, Path(filename).name, src)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="thumbnail generation failed") from None
    return FileResponse(thumb, media_type="image/jpeg")


@router.get("/{platform}/{name}/zip")
async def get_zip(
    platform: str,
    name: str,
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    platform = _require_platform(platform)
    name = _require_name(name)
    pdir = _profile_dir(settings, platform, name)
    if not pdir.is_dir():
        raise HTTPException(status_code=404, detail="profile not found")

    zip_path = settings.data_dir / "zips" / f"{platform}_{name}.zip"
    # Build if missing or older than any source file.
    files = _profile_files(pdir)
    if not files:
        raise HTTPException(status_code=404, detail="profile has no media")
    newest = max(f.stat().st_mtime for f in files)
    if not zip_path.exists() or zip_path.stat().st_mtime < newest:
        _build_zip(zip_path, pdir, files)

    os.utime(zip_path, None)  # refresh mtime -> TTL counts from last access
    return FileResponse(zip_path, media_type="application/zip", filename=f"{name}.zip")


@router.delete("/{platform}/{name}", status_code=204)
async def delete_profile(
    platform: str,
    name: str,
    store: ProfileStore = Depends(get_profile_store),
    settings: Settings = Depends(get_settings),
) -> None:
    platform = _require_platform(platform)
    name = _require_name(name)
    if not _profile_dir(settings, platform, name).exists():
        raise HTTPException(status_code=404, detail="profile not found")
    await store.delete(platform, name)


def _profile_files(pdir: Path) -> list[Path]:
    out: list[Path] = []
    for root, _dirs, files in os.walk(pdir):
        for fn in files:
            if fn.endswith(_HIDDEN_SUFFIXES):
                continue
            out.append(Path(root) / fn)
    return out


def _build_zip(zip_path: Path, pdir: Path, files: list[Path]) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    # Temp file in the SAME dir so os.replace works (no cross-device rename from /tmp).
    with (
        tempfile.NamedTemporaryFile(
            dir=zip_path.parent, prefix=zip_path.name + "-", suffix=".tmp", delete=False
        ) as tmp,
        zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf,
    ):
        for full in files:
            zf.write(full, arcname=full.relative_to(pdir))
    os.replace(tmp.name, zip_path)
