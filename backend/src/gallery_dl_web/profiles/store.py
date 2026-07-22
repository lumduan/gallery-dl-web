"""Per-profile metadata index.

The source of truth is the files gallery-dl writes under ``<downloads>/<platform>/<name>/``. After
each job the manager calls ``reconcile(platform, name)``, which walks that folder, classifies each
file as image/video, picks an avatar, and atomically writes ``metadata.json`` under
``<data_dir>/profiles/<platform>/<name>/``. Keeping metadata OUT of ``downloads/`` means it never
shows up in ``GET /api/files`` and never lands inside a profile .zip, and gallery-dl can't clobber
it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, cast

from gallery_dl_web.config import Settings

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v"}
_AVATAR_HINT = "avatar"
_METADATA_NAME = "metadata.json"
_PLATFORMS = ("instagram", "facebook")


class ProfileStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._root = settings.data_dir / "profiles"
        self._downloads = settings.downloads_dir
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    # ------------------------------------------------------------------ paths

    def _profile_dir(self, platform: str, name: str) -> Path:
        return self._downloads / platform / name

    def _meta_path(self, platform: str, name: str) -> Path:
        return self._root / platform / name / _METADATA_NAME

    def _lock_for(self, platform: str, name: str) -> asyncio.Lock:
        key = (platform, name)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    # ------------------------------------------------------------------ reconcile

    async def reconcile(
        self, platform: str, name: str, archive_path: str | None = None
    ) -> dict[str, Any] | None:
        """Rebuild metadata.json for one profile from its on-disk files. Returns the metadata."""
        async with self._lock_for(platform, name):
            return await asyncio.to_thread(self._reconcile_sync, platform, name, archive_path)

    def _reconcile_sync(
        self, platform: str, name: str, archive_path: str | None = None
    ) -> dict[str, Any] | None:
        pdir = self._profile_dir(platform, name)
        if not pdir.is_dir():
            return None
        images: list[dict[str, Any]] = []
        videos: list[dict[str, Any]] = []
        for root, _dirs, files in os.walk(pdir):
            for fn in files:
                full = Path(root) / fn
                ext = full.suffix.lower()
                kind = "image" if ext in _IMAGE_EXTS else "video" if ext in _VIDEO_EXTS else None
                if kind is None:
                    continue
                try:
                    st = full.stat()
                except OSError:
                    continue
                entry = {
                    "filename": fn,
                    "path": str(full.relative_to(self._downloads)),
                    "bytes": st.st_size,
                    "mtime": st.st_mtime,
                    "kind": kind,
                }
                (images if kind == "image" else videos).append(entry)
        images.sort(key=lambda e: e["mtime"], reverse=True)
        videos.sort(key=lambda e: e["mtime"], reverse=True)
        meta = {
            "platform": platform,
            "name": name,
            "avatar": _pick_avatar(images),
            "archive_path": archive_path,
            "images": images,
            "videos": videos,
            "image_count": len(images),
            "video_count": len(videos),
            "total_bytes": sum(e["bytes"] for e in images + videos),
            "last_updated": time.time(),
        }
        self._save_atomic(self._meta_path(platform, name), meta)
        return meta

    @staticmethod
    def _save_atomic(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError as exc:
            logger.warning("could not chmod %s: %s", tmp, exc)
        os.replace(tmp, path)

    # ------------------------------------------------------------------ read

    def load(self, platform: str, name: str) -> dict[str, Any] | None:
        path = self._meta_path(platform, name)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.exception("failed to read metadata %s", path)
            return None
        return cast(dict[str, Any], data) if isinstance(data, dict) else None

    async def load_or_rebuild(self, platform: str, name: str) -> dict[str, Any] | None:
        meta = self.load(platform, name)
        if meta is not None:
            return meta
        return await self.reconcile(platform, name)

    async def list_all(self) -> list[dict[str, Any]]:
        """Every profile (platform, name) that currently has media on disk."""
        out: list[dict[str, Any]] = []
        if not self._downloads.is_dir():
            return out
        for platform in _PLATFORMS:
            platform_dir = self._downloads / platform
            if not platform_dir.is_dir():
                continue
            for name_dir in sorted(platform_dir.iterdir()):
                if not name_dir.is_dir():
                    continue
                meta = await self.load_or_rebuild(platform, name_dir.name)
                if meta and (meta["image_count"] or meta["video_count"]):
                    out.append(meta)
        return out

    # ------------------------------------------------------------------ delete

    async def delete(self, platform: str, name: str) -> bool:
        """Remove a profile's files, thumbnails, metadata, per-profile archive, and zip."""
        meta = self.load(platform, name) or {}
        targets = [
            self._downloads / platform / name,
            self._settings.data_dir / "thumbnails" / platform / name,
            self._root / platform / name,
            self._settings.data_dir / "archive" / platform / f"{name}.sqlite",
            self._settings.data_dir / "zips" / f"{platform}_{name}.zip",
        ]
        # The per-profile archive is keyed by the URL username (extract_username), which usually
        # differs from gallery-dl's {username} folder name — so read the real path from metadata.
        if meta.get("archive_path"):
            targets.append(Path(str(meta["archive_path"])))
        removed = False
        for t in targets:
            try:
                if t.is_dir():
                    shutil.rmtree(t)
                    removed = True
                elif t.is_file():
                    t.unlink()
                    removed = True
            except OSError as exc:
                logger.warning("could not remove %s: %s", t, exc)
        return removed


def _pick_avatar(images: list[dict[str, Any]]) -> str | None:
    for entry in images:
        if _AVATAR_HINT in entry["filename"].lower():
            return str(entry["path"])
    return str(images[0]["path"]) if images else None
