from __future__ import annotations

import time
from pathlib import Path

from PIL import Image

from gallery_dl_web.config import Settings
from gallery_dl_web.profiles.thumbnails import get_or_generate, is_image


def _settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    return Settings(
        data_dir=data,
        downloads_dir=data / "dl",
        cookies_path=data / "c.json",
        cors_origins=["http://x"],
        max_concurrent_jobs=1,
        thumbnail_size=64,
    )


def _make_img(path: Path, color: str = "red", size: tuple[int, int] = (200, 120)) -> None:
    Image.new("RGB", size, color).save(path)


def test_is_image() -> None:
    assert is_image("a.jpg")
    assert is_image("b.PNG")
    assert not is_image("c.mp4")


def test_generate_and_cache(tmp_path: Path) -> None:
    src = tmp_path / "src.jpg"
    _make_img(src)
    out = get_or_generate(_settings(tmp_path), "facebook", "susan", "src.jpg", src)
    assert out.exists()
    with Image.open(out) as im:
        assert max(im.size) <= 64
    out2 = get_or_generate(_settings(tmp_path), "facebook", "susan", "src.jpg", src)
    assert out2 == out  # cache hit, same path


def test_regenerates_when_source_newer(tmp_path: Path) -> None:
    src = tmp_path / "src.jpg"
    _make_img(src, "red")
    out = get_or_generate(_settings(tmp_path), "facebook", "susan", "src.jpg", src)
    first_mtime = out.stat().st_mtime
    time.sleep(0.05)
    _make_img(src, "blue")
    out2 = get_or_generate(_settings(tmp_path), "facebook", "susan", "src.jpg", src)
    assert out2.stat().st_mtime >= first_mtime
