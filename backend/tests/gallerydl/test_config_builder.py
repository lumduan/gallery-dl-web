from __future__ import annotations

from typing import Any

import pytest

from gallery_dl_web.gallerydl import config_builder

PathKey = tuple[tuple[str, ...], str]


class FakeConfig:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str, Any]] = []

    def set(self, path: tuple[str, ...], key: str, value: Any) -> None:
        self.calls.append((path, key, value))

    def as_dict(self) -> dict[PathKey, Any]:
        return {(p, k): v for (p, k, v) in self.calls}


def _ig(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "platform": "instagram",
        "output_dir": "/out",
        "cookies": {"sessionid": "S"},
        "options": {},
    }
    base.update(over)
    return base


def _fb(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "platform": "facebook",
        "output_dir": "/out",
        "cookies": {"c_user": "1"},
        "options": {},
    }
    base.update(over)
    return base


def test_instagram_defaults() -> None:
    fake = FakeConfig()
    config_builder.apply(_ig(), fake)
    d = fake.as_dict()
    assert d[(("extractor", "instagram"), "cookies")] == {"sessionid": "S"}
    assert d[(("extractor",), "base-directory")] == "/out"
    assert d[(("extractor",), "cookies-update")] is False
    assert d[(("extractor", "instagram"), "sleep-request")] == [6.0, 12.0]
    assert d[(("extractor", "instagram"), "directory")] == ["instagram", "{username}"]
    assert d[(("extractor", "instagram"), "videos")] is True


def test_options_override_defaults() -> None:
    fake = FakeConfig()
    config_builder.apply(_ig(options={"include": "posts", "filename": "x.{extension}"}), fake)
    d = fake.as_dict()
    assert d[(("extractor", "instagram"), "include")] == "posts"
    assert d[(("extractor", "instagram"), "filename")] == "x.{extension}"
    # Unchanged defaults still present.
    assert d[(("extractor", "instagram"), "videos")] is True


def test_archive_option_set() -> None:
    fake = FakeConfig()
    config_builder.apply(_ig(options={"archive": "/a/ig.sqlite"}), fake)
    assert fake.as_dict()[(("extractor", "instagram"), "archive")] == "/a/ig.sqlite"


def test_facebook_defaults() -> None:
    fake = FakeConfig()
    config_builder.apply(_fb(), fake)
    d = fake.as_dict()
    assert d[(("extractor", "facebook"), "cookies")] == {"c_user": "1"}
    assert d[(("extractor", "facebook"), "videos")] == "ytdl"
    assert d[(("extractor", "facebook"), "filename")] == "{id}.{extension}"


def test_facebook_cookies_as_file_path() -> None:
    fake = FakeConfig()
    config_builder.apply(_fb(cookies="/tmp/cookies.txt"), fake)
    assert fake.as_dict()[(("extractor", "facebook"), "cookies")] == "/tmp/cookies.txt"


def test_unsupported_platform() -> None:
    with pytest.raises(ValueError, match="unsupported platform"):
        config_builder.apply(
            {"platform": "twitter", "output_dir": "/out", "cookies": {}, "options": {}},
            FakeConfig(),
        )


def test_instagram_missing_sessionid() -> None:
    with pytest.raises(ValueError, match="sessionid"):
        config_builder.apply(
            {"platform": "instagram", "output_dir": "/out", "cookies": {}, "options": {}},
            FakeConfig(),
        )


def test_facebook_missing_cookies() -> None:
    with pytest.raises(ValueError, match="facebook requires cookies"):
        config_builder.apply(
            {"platform": "facebook", "output_dir": "/out", "cookies": None, "options": {}},
            FakeConfig(),
        )


def test_returns_call_tree() -> None:
    fake = FakeConfig()
    calls = config_builder.apply(_ig(), fake)
    assert calls == fake.calls
    assert len(calls) >= 6
