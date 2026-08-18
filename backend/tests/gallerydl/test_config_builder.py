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


def test_facebook_is_paced() -> None:
    """Facebook blocks an unpaced account ("temporarily blocked from viewing images")."""
    fake = FakeConfig()
    config_builder.apply(_fb(), fake)
    lo, hi = fake.as_dict()[(("extractor", "facebook"), "sleep-request")]
    assert 0 < lo <= hi


def test_sleep_request_is_overridable_per_job() -> None:
    fake = FakeConfig()
    config_builder.apply(_fb(options={"sleep-request": [30.0, 45.0]}), fake)
    assert fake.as_dict()[(("extractor", "facebook"), "sleep-request")] == [30.0, 45.0]


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


def test_avatar_include_appended() -> None:
    fake = FakeConfig()
    config_builder.apply(_ig(options={"include_avatar": True}), fake)
    inc = fake.as_dict()[(("extractor", "instagram"), "include")]
    assert "avatar" in inc.split(",")


def test_avatar_include_idempotent() -> None:
    fake = FakeConfig()
    config_builder.apply(_ig(options={"include_avatar": True, "include": "posts,avatar"}), fake)
    inc = fake.as_dict()[(("extractor", "instagram"), "include")]
    assert inc.count("avatar") == 1


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


# ----------------------------------------------------------------- anonymous (cookie-free) mode


def test_anonymous_instagram_sends_no_cookies_and_switches_to_graphql() -> None:
    """Logged-out, IG's REST /api/v1/* endpoints mostly 401; GraphQL is the path that answers."""
    fake = FakeConfig()
    config_builder.apply(_ig(anonymous=True, cookies=None), fake)
    d = fake.as_dict()
    # None is falsy, so gallery-dl's `if cookies := self.config("cookies")` guard no-ops.
    assert d[(("extractor", "instagram"), "cookies")] is None
    assert d[(("extractor", "instagram"), "api")] == "graphql"


def test_anonymous_instagram_strips_auth_only_include() -> None:
    """stories/highlights abort the whole extraction logged-out — not merely come back empty."""
    fake = FakeConfig()
    config_builder.apply(
        _ig(anonymous=True, cookies=None, options={"include": "posts,stories,reels,highlights"}),
        fake,
    )
    assert fake.as_dict()[(("extractor", "instagram"), "include")] == "posts,reels"


def test_anonymous_instagram_include_of_only_auth_categories_falls_back() -> None:
    fake = FakeConfig()
    config_builder.apply(
        _ig(anonymous=True, cookies=None, options={"include": "stories,highlights"}), fake
    )
    assert fake.as_dict()[(("extractor", "instagram"), "include")] == "posts"


def test_anonymous_avatar_append_uses_the_filtered_include() -> None:
    """The avatar block appends to the RESOLVED include; re-deriving it would undo the filtering."""
    fake = FakeConfig()
    config_builder.apply(
        _ig(
            anonymous=True,
            cookies=None,
            options={"include": "posts,stories", "include_avatar": True},
        ),
        fake,
    )
    assert fake.as_dict()[(("extractor", "instagram"), "include")] == "posts,avatar"


def test_anonymous_instagram_explicit_api_option_wins() -> None:
    fake = FakeConfig()
    config_builder.apply(_ig(anonymous=True, cookies=None, options={"api": "rest"}), fake)
    assert fake.as_dict()[(("extractor", "instagram"), "api")] == "rest"


def test_cookied_instagram_gets_no_api_override() -> None:
    """The tuning is anonymous-only: a cookied job keeps gallery-dl's own default API."""
    fake = FakeConfig()
    config_builder.apply(_ig(options={"include": "posts,stories"}), fake)
    d = fake.as_dict()
    assert (("extractor", "instagram"), "api") not in d
    assert d[(("extractor", "instagram"), "include")] == "posts,stories"  # not filtered


def test_anonymous_facebook_needs_no_cookies_and_no_tuning() -> None:
    fake = FakeConfig()
    config_builder.apply(_fb(anonymous=True, cookies=None), fake)
    d = fake.as_dict()
    assert d[(("extractor", "facebook"), "cookies")] is None
    assert (("extractor", "facebook"), "api") not in d
    # Facebook's include is never filtered — its categories all work logged-out on public content.
    assert d[(("extractor", "facebook"), "include")] == "photos"
    # Pacing still applies: logged-out requests are rate-limited by IP instead of by account.
    assert d[(("extractor", "facebook"), "sleep-request")] == [3.0, 8.0]


def test_anonymous_still_rejects_an_unsupported_platform() -> None:
    with pytest.raises(ValueError, match="unsupported platform"):
        config_builder.apply(
            {"platform": "twitter", "output_dir": "/out", "cookies": None, "anonymous": True},
            FakeConfig(),
        )


def test_facebook_albums_are_opt_in() -> None:
    """`albums` re-walks the photos `photos` already covered, and the archive is only checked
    AFTER each 1-3 MB page is fetched — so it roughly doubles wall-clock for no new files."""
    fake = FakeConfig()
    config_builder.apply(_fb(), fake)
    assert fake.as_dict()[(("extractor", "facebook"), "include")] == "photos"

    fake = FakeConfig()
    config_builder.apply(_fb(options={"include": "photos,albums"}), fake)
    assert fake.as_dict()[(("extractor", "facebook"), "include")] == "photos,albums"


def test_facebook_fallback_stall_is_bounded() -> None:
    """Two consecutive unparseable photos at gallery-dl's defaults (2 x 61 s each) would blow the
    manager's progress deadline and get a healthy job killed and re-walked from the top."""
    fake = FakeConfig()
    config_builder.apply(_fb(), fake)
    d = fake.as_dict()
    assert d[(("extractor", "facebook"), "fallback-retries")] == 1
    # Shaped, not flattened: downloader/http.py inherits this for CDN 429s.
    assert d[(("extractor", "facebook"), "sleep-429")] == "exponential:2:0:60=15"


def test_adaptive_pacing_seeds_sleep_request_with_the_floor() -> None:
    """In adaptive mode the worker's pacer owns the delay; `sleep-request` is only the fallback
    for a run where the pacer never installs, and the floor is the right thing to fall back to."""
    fake = FakeConfig()
    config_builder.apply(_fb(pacing={"mode": "adaptive", "min": 1.5, "max": 30.0}), fake)
    assert fake.as_dict()[(("extractor", "facebook"), "sleep-request")] == [1.5, 1.5]


def test_fixed_pacing_passes_the_range_through() -> None:
    fake = FakeConfig()
    config_builder.apply(_fb(pacing={"mode": "fixed", "min": 3.0, "max": 8.0}), fake)
    assert fake.as_dict()[(("extractor", "facebook"), "sleep-request")] == [3.0, 8.0]


def test_an_explicit_sleep_request_beats_the_pacing_block() -> None:
    """The raw gallery-dl escape hatch stays the highest-precedence knob."""
    fake = FakeConfig()
    config_builder.apply(
        _fb(
            pacing={"mode": "adaptive", "min": 1.0, "max": 30.0},
            options={"sleep-request": [11.0, 12.0]},
        ),
        fake,
    )
    assert fake.as_dict()[(("extractor", "facebook"), "sleep-request")] == [11.0, 12.0]


def test_quick_update_sets_a_consecutive_skip_limit() -> None:
    """Facebook walks newest-first, so a refresh re-fetches every old page purely to skip it."""
    fake = FakeConfig()
    config_builder.apply(_fb(), fake)
    assert (("extractor", "facebook"), "skip") not in fake.as_dict()

    fake = FakeConfig()
    config_builder.apply(_fb(options={"quick_update": True}), fake)
    # `terminate`, not `abort`: a parent dispatching several extractors must still run the rest.
    assert fake.as_dict()[(("extractor", "facebook"), "skip")] == "terminate:20"

    fake = FakeConfig()
    config_builder.apply(_fb(options={"quick_update": 5}), fake)
    assert fake.as_dict()[(("extractor", "facebook"), "skip")] == "terminate:5"


def test_quick_update_ignores_nonsense_instead_of_failing_the_job() -> None:
    for bad in ("", "abc", 0, -3, None, False):
        fake = FakeConfig()
        config_builder.apply(_fb(options={"quick_update": bad}), fake)
        assert (("extractor", "facebook"), "skip") not in fake.as_dict()


def test_a_malformed_pacing_block_falls_back_instead_of_failing_the_job() -> None:
    """Pacing is a hint. config_builder is the load-bearing translator and must not raise here —
    the worker's own guard runs later, so a crash here would kill the job outright."""
    for bad in ({"mode": "adaptive", "min": "x", "max": 30.0}, {"mode": "adaptive"}, {}):
        fake = FakeConfig()
        config_builder.apply(_fb(pacing=bad), fake)
        assert fake.as_dict()[(("extractor", "facebook"), "sleep-request")] == [3.0, 8.0]
