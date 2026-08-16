"""Translate a job payload into gallery-dl ``config.set(...)`` calls.

This is a pure translator (no I/O, no network) — it accepts an injectable ``config`` object so
unit tests can record the exact call tree without importing gallery-dl's globals. The worker
passes the real ``gallery_dl.config`` module; tests pass a recorder.

gallery-dl's ``config.set`` signature is ``set(path, key, value)`` where ``path`` is a tuple of
config sections (e.g. ``("extractor", "instagram")``) and ``key`` is the option name.

Payload shape (received by the worker over stdin)::

    {
      "job_id", "url", "platform", "output_dir",
      "anonymous": bool,   # run logged-out; `cookies` is then None and never validated
      "cookies": {"sessionid": "..."} (IG) | {name: value, ...} (FB) | None,
      "options": {"include", "videos", "sleep_request", "directory", "filename", "archive", "api"},
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
    # Facebook blocks an account that fetches images back-to-back ("You've been temporarily blocked
    # from viewing images" after ~767 in one run). Lower than Instagram's range because Facebook
    # issues a page request per photo, so the delay compounds. The manager overrides this from
    # Settings; this default keeps direct worker invocations paced too.
    "sleep-request": [3.0, 8.0],
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

# Instagram `include` categories that CANNOT work logged-out, and do not merely come back empty —
# they abort the whole extraction. gallery-dl's GraphQL API maps reels_media / highlights_media /
# guide / user_saved / user_collection to `_unsupported` (AbortExtraction), and the REST endpoints
# behind them 401 without a session. Dropping them keeps an anonymous profile walk alive.
_IG_AUTH_ONLY_INCLUDE = frozenset(
    {"stories", "highlights", "stories-tray", "saved", "collection", "followers", "following"}
)


def apply(payload: dict[str, Any], config: ConfigLike) -> list[tuple[ConfigPath, str, Any]]:
    """Apply the payload's settings to ``config`` and return the recorded call tree.

    Raises ValueError if the platform is unsupported, or if cookies are missing on a *cookied* job
    (``anonymous`` is falsy) — the worker catches this and emits a ``failed`` event with a clear
    message. An anonymous job never validates cookies.
    """
    platform = payload["platform"]
    if platform not in _PLATFORM_DEFAULTS:
        raise ValueError(f"unsupported platform: {platform!r}")

    output_dir = str(payload["output_dir"])
    options = payload.get("options") or {}
    cookies = payload.get("cookies")
    anonymous = bool(payload.get("anonymous"))

    calls: list[tuple[ConfigPath, str, Any]] = []

    def _set(path: ConfigPath, key: str, value: Any) -> None:
        config.set(path, key, value)
        calls.append((path, key, value))

    # Global extractor defaults.
    _set(_EXTRACTOR_PATH, "base-directory", output_dir)
    # gallery-dl writes refreshed cookies back to the source by default; never want that here.
    _set(_EXTRACTOR_PATH, "cookies-update", False)
    # Per-request HTTP timeout so a hung request fails fast instead of parking in D state.
    _set(_EXTRACTOR_PATH, "timeout", float(payload.get("http_timeout_seconds", 30)))

    platform_path = _IG_PATH if platform == "instagram" else _FB_PATH
    # Set BEFORE the defaults loop so an `options` key can never overwrite it. In anonymous mode the
    # value is None: gallery-dl's Extractor._init_cookies is guarded by `if cookies := ...`, so a
    # falsy value is a clean no-op rather than an error, and the explicit call documents the intent.
    _set(platform_path, "cookies", None if anonymous else _cookie_for(platform, cookies))

    # Resolve `include` ONCE, here: the avatar block below appends to it, and re-deriving it there
    # from raw `options` would silently undo the anonymous filtering.
    resolved_include = _resolve_include(platform, options, anonymous)

    for key, default in _PLATFORM_DEFAULTS[platform].items():
        value = resolved_include if key == "include" else options.get(key, default)
        _set(platform_path, key, value)

    # Logged-out, Instagram's REST /api/v1/* endpoints mostly 401; the GraphQL query_hash path is
    # the one that still answers. Facebook needs no equivalent. An explicit option wins.
    if anonymous and platform == "instagram":
        _set(_IG_PATH, "api", options.get("api", "graphql"))

    # Avatar: when requested (profile downloads), append 'avatar' to include (idempotent) so
    # gallery-dl also fetches the profile picture for the card.
    if options.get("include_avatar"):
        parts = [p.strip() for p in resolved_include.split(",") if p.strip()]
        if "avatar" not in parts:
            parts.append("avatar")
        _set(platform_path, "include", ",".join(parts))

    if options.get("archive"):
        _set(platform_path, "archive", str(options["archive"]))

    return calls


def _resolve_include(platform: str, options: dict[str, Any], anonymous: bool) -> str:
    """The final `include`: the caller's value (or default), minus what logged-out can't reach."""
    include = str(options.get("include", _PLATFORM_DEFAULTS[platform]["include"]))
    if not (anonymous and platform == "instagram"):
        return include
    parts = [p.strip() for p in include.split(",") if p.strip()]
    kept = [p for p in parts if p.lower() not in _IG_AUTH_ONLY_INCLUDE]
    # An include of only auth-only categories would leave gallery-dl nothing to walk at all.
    return ",".join(kept) if kept else "posts"


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
