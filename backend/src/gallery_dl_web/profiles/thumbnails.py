"""On-demand Pillow thumbnails, mtime-cached under ``<data_dir>/thumbnails/<platform>/<name>/``."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PIL import Image

from gallery_dl_web.config import Settings

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


def thumbnail_path(settings: Settings, platform: str, name: str, filename: str) -> Path:
    return settings.data_dir / "thumbnails" / platform / name / filename


def get_or_generate(
    settings: Settings, platform: str, name: str, filename: str, source: Path
) -> Path:
    """Return a cached thumbnail for ``source``, generating one (JPEG) if missing/stale.

    Idempotent under concurrent callers: both may write, the atomic os.replace wins.
    Raises OSError/Image.UnidentifiedImageError on failure (caller maps to HTTP error).
    """
    cache = thumbnail_path(settings, platform, name, filename)
    try:
        src_mtime = source.stat().st_mtime
    except OSError:
        raise
    if cache.exists() and cache.stat().st_mtime >= src_mtime:
        return cache

    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(cache.name + ".tmp")
    try:
        with Image.open(source) as im:
            thumb = im.convert("RGB")
            thumb.thumbnail((settings.thumbnail_size, settings.thumbnail_size))
            thumb.save(tmp, format="JPEG", quality=85)
        os.replace(tmp, cache)
    except Exception:
        tmp.unlink(missing_ok=True)
        logger.exception("thumbnail generation failed for %s", source)
        raise
    return cache
