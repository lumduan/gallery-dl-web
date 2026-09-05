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
      "pacing": {"mode": "adaptive"|"fixed", "min": float, "max": float,
                 "per_file": float | None} | None,
      "cookies": {"sessionid": "..."} (IG) | {name: value, ...} (FB) | None,
      "options": {"include", "videos", "sleep-request", "sleep", "directory", "filename", "archive",
                  "api", "fallback-retries", "quick_update"},
    }

Note the option keys are gallery-dl's own, so they are HYPHENATED (`sleep-request`), not
snake_case — the defaults loop below looks them up by the same name it sets them under.
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
    # Mirrors gallery-dl's own InstagramExtractor.request_interval. Only reached when the payload
    # carries no `pacing` block (a direct worker invocation); the manager always sends one.
    "sleep-request": [6.0, 12.0],
    # Per-IMAGE delay, a different axis from `sleep-request` above. 0 = off, which is what a direct
    # worker invocation with no `pacing` block gets; the manager always resolves a real value.
    "sleep": 0.0,
    "directory": ["instagram", "{username}"],
    "filename": "{date}_{media_id}_{shortcode}.{extension}",
}

_FB_DEFAULTS: dict[str, Any] = {
    # `albums` is deliberately NOT here. FacebookAlbumsExtractor queues a FacebookSetExtractor per
    # album, each doing its own full serial walk over photos `photos` already covered — and
    # DownloadJob.handle_url checks the archive only AFTER the page has been fetched and parsed,
    # so the duplicate walk costs full 1-3 MB page fetches for near-zero new files. Roughly a 2x
    # wall-clock tax. Pass include="photos,albums" per job to get it back.
    "include": "photos",
    "videos": "ytdl",
    # gallery-dl itself ships Facebook with NO pacing (request_interval 0.0); this delay is ours,
    # added after Facebook blocked an account at ~767 images in one run. Only reached when the
    # payload carries no `pacing` block — the manager always sends one.
    "sleep-request": [3.0, 8.0],
    # On a photo whose download URL fails to parse, `extract_set` retries via
    # `self.wait(self._interval_429(n))`. gallery-dl's defaults make that 60 s + wait()'s 1 s
    # adjust, TWICE — 122 s of dead sleep per bad photo, emitting no `prepare` or `file`, so the
    # manager's progress deadline is burning the whole time. Two consecutive bad photos would
    # exceed `stall_floor_seconds` on their own and get a healthy job killed and re-walked.
    "fallback-retries": 1,
    # ...and shape the backoff instead of flattening it: 15 s, then 30 s, then 60 s
    # (`util.build_duration_func_ex` parses "exponential:base:start:max=value"). A plain small
    # number would be actively dangerous — `downloader/http.py` inherits `extractor._interval_429`
    # for CDN 429s, so flattening it to 5 s is how a soft block becomes a hard one. This keeps the
    # long tail for genuine 429s while removing the front-loaded 60 s the fallback path abuses.
    "sleep-429": "exponential:2:0:60=15",
    # Off by default here and in `Settings`: extract_set fetches a full HTML page per photo, so
    # Facebook already pays `sleep-request` once per image and this would double-charge it.
    "sleep": 0.0,
    "directory": ["facebook", "{username}"],
    "filename": "{id}.{extension}",
}

_PLATFORM_DEFAULTS: dict[str, dict[str, Any]] = {
    "instagram": _IG_DEFAULTS,
    "facebook": _FB_DEFAULTS,
}

# Consecutive-skip limit used when `quick_update` is passed as a bare `true`.
_QUICK_UPDATE_DEFAULT = 20

# Per-image delays are emitted as a band, never a constant: a perfectly periodic request pattern is
# trivially fingerprintable, and gallery-dl ships InstagramExtractor.request_interval as a range
# (6.0, 12.0) for the same reason. +/-15% keeps the operator's number as the mean.
_PER_FILE_JITTER = 0.15

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
    resolved_sleep = _resolve_sleep_request(platform, options, payload.get("pacing"))
    resolved_per_file = _resolve_sleep_file(platform, options, payload.get("pacing"))

    for key, default in _PLATFORM_DEFAULTS[platform].items():
        value: Any = options.get(key, default)
        if key == "include":
            value = resolved_include
        elif key == "sleep-request":
            value = resolved_sleep
        elif key == "sleep":
            value = resolved_per_file
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

    # Quick update: stop an extractor after N *consecutive* already-have files. Facebook walks its
    # photo set newest-first, so a refresh reaches the new photos immediately and then re-fetches
    # every old page purely to skip it. `terminate` (not `abort`) stops only the current extractor,
    # so a parent dispatching several still runs the rest.
    # Off by default: a run that was blocked halfway leaves the FRONT of the set archived, so an
    # early stop would hide the un-fetched tail.
    if quick := _quick_update_limit(options):
        _set(platform_path, "skip", f"terminate:{quick}")

    return calls


def _quick_update_limit(options: dict[str, Any]) -> int:
    """The `quick_update` option as a positive int, or 0 when off/invalid."""
    raw = options.get("quick_update")
    if raw is True:
        return _QUICK_UPDATE_DEFAULT
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _resolve_sleep_request(platform: str, options: dict[str, Any], pacing: Any) -> Any:
    """What gallery-dl's own `sleep-request` should be for this job.

    Precedence: an explicit per-job `sleep-request` wins outright (it is the raw gallery-dl escape
    hatch), then the resolved `pacing` block, then the platform default.

    In **adaptive** mode this is set to ``[min, min]`` — the floor, not the range. The worker's
    ``pacing.AdaptivePacer`` overrides ``Extractor._interval_request`` per request and is the real
    source of the delay; this value only matters if the pacer fails to install, and then the floor
    is the right thing to fall back to.
    """
    if "sleep-request" in options:
        return options["sleep-request"]
    if isinstance(pacing, dict):
        try:
            lo, hi = float(pacing["min"]), float(pacing["max"])
        except (KeyError, TypeError, ValueError):
            # Pacing is a hint, not a contract. A malformed block must not fail the job — the
            # platform default below is always a usable answer.
            return _PLATFORM_DEFAULTS[platform]["sleep-request"]
        lo, hi = max(0.0, lo), max(0.0, hi)
        return [lo, lo] if pacing.get("mode") == "adaptive" else [lo, max(lo, hi)]
    return _PLATFORM_DEFAULTS[platform]["sleep-request"]


def _resolve_sleep_file(platform: str, options: dict[str, Any], pacing: Any) -> Any:
    """What gallery-dl's per-download `sleep` should be for this job.

    Precedence mirrors ``_resolve_sleep_request``: an explicit per-job `sleep` is the raw gallery-dl
    escape hatch and wins outright, then the resolved `pacing` block, then the platform default.

    Unlike `sleep-request` this is **additive**, not a spacing floor — ``DownloadJob.handle_url``
    calls ``extractor.sleep(self.sleep(), "download")`` and ``Extractor.sleep`` is a bare
    ``time.sleep``. So the gap an operator observes is this value plus the download itself, which is
    why the band is centred on their number rather than starting at it.

    It is charged only on files actually fetched: both the archive check and the on-disk check in
    ``handle_url`` return before the sleep, so re-running a downloaded profile pays nothing. And 0
    is a true off switch — ``util.build_duration_func(0.0)`` returns None and gallery-dl skips the
    call site entirely.
    """
    if "sleep" in options:
        return options["sleep"]
    seconds = 0.0
    if isinstance(pacing, dict):
        try:
            seconds = max(0.0, float(pacing.get("per_file") or 0.0))
        except (TypeError, ValueError):
            # Same contract as the pacing block above: a hint, never a reason to fail the job.
            return _PLATFORM_DEFAULTS[platform]["sleep"]
    if not seconds:
        return 0.0
    return [
        round(seconds * (1.0 - _PER_FILE_JITTER), 2),
        round(seconds * (1.0 + _PER_FILE_JITTER), 2),
    ]


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
