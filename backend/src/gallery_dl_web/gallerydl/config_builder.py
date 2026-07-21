"""Translate a job payload into gallery-dl ``config.set(...)`` calls.

This is a pure translator (no I/O, no network) — it accepts an injectable ``config`` object so
unit tests can record the exact call tree without importing gallery-dl's globals. The worker
passes the real ``gallery_dl.config`` module; tests pass a recorder.

gallery-dl's ``config.set`` signature is ``set(path, key, value)`` where ``path`` is a tuple of
config sections (e.g. ``("extractor", "instagram")``) and ``key`` is the option name.

Payload shape (received by the worker over stdin)::

    {
      "job_id", "url", "platform", "output_dir",
      "cookies": {"sessionid": "..."} (IG) | {name: value, ...} (FB),
      "options": {"include", "videos", "sleep_request", "directory", "filename", "archive"},
    }
"""

from __future__ import annotations

from typing import Any, Protocol

type ConfigPath = tuple[str, ...]


class ConfigLike(Protocol):
    """Minimal subset of gallery_dl.config we depend on: ``set(path, key, value)``."""

    def set(self, path: ConfigPath, key: str, value: Any) -> None: ...


_IG_DEFAULTS: dict[str, Any] = {
    "include": "posts,reels",
    "videos": True,
    "sleep-request": [6.0, 12.0],
    "directory": ["instagram", "{username}"],
    "filename": "{date}_{media_id}_{shortcode}.{extension}",
}

_FB_DEFAULTS: dict[str, Any] = {
    "include": "photos,albums",
    "videos": "ytdl",
    "directory": ["facebook", "{username}"],
    "filename": "{id}.{extension}",
}

_PLATFORM_DEFAULTS: dict[str, dict[str, Any]] = {
    "instagram": _IG_DEFAULTS,
    "facebook": _FB_DEFAULTS,
}

_IG_PATH: ConfigPath = ("extractor", "instagram")
_FB_PATH: ConfigPath = ("extractor", "facebook")
_EXTRACTOR_PATH: ConfigPath = ("extractor",)


def apply(payload: dict[str, Any], config: ConfigLike) -> list[tuple[ConfigPath, str, Any]]:
    """Apply the payload's settings to ``config`` and return the recorded call tree.

    Raises ValueError if the platform is unsupported or required cookies are missing — the
    worker catches this and emits a ``failed`` event with a clear message.
    """
    platform = payload["platform"]
    if platform not in _PLATFORM_DEFAULTS:
        raise ValueError(f"unsupported platform: {platform!r}")

    output_dir = str(payload["output_dir"])
    options = payload.get("options") or {}
    cookies = payload.get("cookies")

    calls: list[tuple[ConfigPath, str, Any]] = []

    def _set(path: ConfigPath, key: str, value: Any) -> None:
        config.set(path, key, value)
        calls.append((path, key, value))

    # Global extractor defaults.
    _set(_EXTRACTOR_PATH, "base-directory", output_dir)
    # gallery-dl writes refreshed cookies back to the source by default; never want that here.
    _set(_EXTRACTOR_PATH, "cookies-update", False)

    platform_path = _IG_PATH if platform == "instagram" else _FB_PATH
    cookie_arg = _cookie_for(platform, cookies)
    _set(platform_path, "cookies", cookie_arg)

    for key, default in _PLATFORM_DEFAULTS[platform].items():
        _set(platform_path, key, options.get(key, default))

    if options.get("archive"):
        _set(platform_path, "archive", str(options["archive"]))

    return calls


def _cookie_for(platform: str, cookies: Any) -> Any:
    """Validate + normalize the cookie payload for the platform."""
    if platform == "instagram":
        sessionid = ""
        if isinstance(cookies, dict):
            sessionid = str(cookies.get("sessionid") or "")
        if not sessionid:
            raise ValueError(
                "instagram requires a 'sessionid' cookie (gallery-dl disables password login)"
            )
        return {"sessionid": sessionid}

    if platform == "facebook":
        # gallery-dl accepts a Netscape file path (str) or a name/value dict.
        if isinstance(cookies, str) and cookies:
            return cookies
        if isinstance(cookies, dict) and cookies:
            return cookies
        raise ValueError("facebook requires cookies (a Netscape cookies.txt or name/value map)")

    # Unreachable: apply() guards platform first.
    raise ValueError(f"unsupported platform: {platform!r}")
