"""Notice when a platform logs the stored session out, and say so on stderr.

**OBSERVED 2026-11-19.** A stored Instagram session that Instagram had logged out looks like this
from the inside, on the very first request of the run:

    GET /<profile>/   302  -> https://www.instagram.com/
                      Set-Cookie: sessionid=<placeholder>; expires=Thu, 01 Jan 1970 …; Max-Age=-…
    GET /             200  (the home page)
    -> AbortExtraction: HTTP redirect to home page

That last line is word for word what the 2026-08-26 rate-limit block produced, so the run was
reported as "rate-limited, wait (typically hours)" — and waiting can never bring back a session the
platform has deleted. The same profile answered 200 with no cookies at all, which rules out a block
of the IP or of the profile. The one thing that tells the two apart is the ``Set-Cookie`` header
*deleting* ``sessionid``, and neither sensor could see it: stderr carries only gallery-dl's
wording, and the pacer's signature table judged status, URL and body only.

This module attaches a ``requests`` response hook to every extractor session and logs one WARNING
carrying ``errors.SESSION_LOGGED_OUT_MARKER`` when a session cookie is deleted;
``errors.detect_session_logged_out`` reads it back on the manager's side. It is installed for
**every** run, unlike ``pacing``'s hook, which ``fixed`` mode never installs. ``pacing`` also calls
``deleted_cookies`` so the pushback names the right rule.

It reads and logs cookie **names** only. A deletion's value is a placeholder, but the hook sees
every ``Set-Cookie`` of every response, including the ones that issue real tokens, and the
decision needs nothing from the value.

Same posture as the other worker-side installs: patch the class (profile extraction spawns child
extractors with their own sessions), never raise out of the hook, and degrade to "not installed".
"""

from __future__ import annotations

import contextlib
import email.utils
import functools
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from gallery_dl_web.gallerydl.errors import SESSION_LOGGED_OUT_MARKER

logger = logging.getLogger(__name__)

# (host suffix, the cookies whose deletion means "this session is logged out"). Instagram only:
# that is the one that has been observed. Facebook's `c_user`/`xs` would be the analogue, but a rule
# that sends the operator to re-export cookies must not be guessed.
SESSION_COOKIES: tuple[tuple[str, tuple[str, ...]], ...] = (("instagram.com", ("sessionid",)),)

_INSTALLED: Any = None


def _set_cookie_headers(response: Any) -> list[str]:
    """Every ``Set-Cookie`` line, unmerged.

    ``response.headers`` folds repeated headers into one comma-joined string, and ``Expires``
    values contain commas, so that form cannot be split back apart. ``response.cookies`` is no
    use either: ``http.cookiejar`` drops an already-expired cookie instead of storing it, which is
    exactly the one we are looking for. urllib3's header dict keeps each line.
    """
    headers = getattr(getattr(response, "raw", None), "headers", None)
    getlist = getattr(headers, "getlist", None)
    if getlist is None:
        return []
    return [str(line) for line in getlist("Set-Cookie")]


_NETSCAPE_DATE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{2,4})")


def _parse_expires(value: str) -> float | None:
    """Epoch seconds of an ``Expires`` value, or None. Accepts RFC 1123 and Netscape's dashed form.

    Returns ``0`` for the 1970 epoch, which is falsy, so callers must test against None.
    """
    parsed = email.utils.parsedate_tz(_NETSCAPE_DATE.sub(r"\1 \2 \3", value.strip()))
    return None if parsed is None else float(email.utils.mktime_tz(parsed))


def _deletes(attributes: list[str], now: float) -> bool:
    """True if a cookie's attributes expire it. ``Max-Age`` outranks ``Expires`` (RFC 6265 5.3)."""
    expires: float | None = None
    for attribute in attributes:
        key, _, value = attribute.strip().partition("=")
        key = key.strip().lower()
        if key == "max-age":
            try:
                return int(value.strip()) <= 0
            except ValueError:
                continue
        if key == "expires":
            expires = _parse_expires(value)
    return expires is not None and expires <= now


def deleted_cookies(response: Any) -> tuple[str, ...]:
    """Names of the cookies this response deletes, in header order. Never raises, never a value."""
    try:
        now = time.time()
        names: list[str] = []
        for line in _set_cookie_headers(response):
            first, *attributes = line.split(";")
            name = first.partition("=")[0].strip()
            if name and _deletes(attributes, now):
                names.append(name)
        return tuple(names)
    except Exception:  # noqa: BLE001 — a sensor must never fail the request it is watching
        return ()


def session_logged_out(response: Any) -> tuple[str, str] | None:
    """``(host suffix, cookie name)`` if this response deletes a platform's session cookie."""
    try:
        host = (urlsplit(str(response.url)).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return None
    for suffix, names in SESSION_COOKIES:
        if host == suffix or host.endswith("." + suffix):
            for name in deleted_cookies(response):
                if name in names:
                    return suffix, name
    return None


def install() -> Callable[[], None]:
    """Attach the hook to every extractor session. Idempotent; returns a callable that restores."""
    global _INSTALLED
    if _INSTALLED is not None:
        return _INSTALLED  # type: ignore[no-any-return]

    from gallery_dl.extractor.common import Extractor

    original = Extractor._init_session
    reported: set[tuple[str, str]] = set()
    lock = threading.Lock()

    def _hook(response: Any, *_args: Any, **_kwargs: Any) -> None:
        # Returning anything but None would REPLACE the response, and raising escapes
        # Session.send, so this swallows everything. Once per cookie per process: every child
        # extractor reloads the stored cookie from config and is logged out again.
        with contextlib.suppress(Exception):
            hit = session_logged_out(response)
            if hit is None:
                return
            with lock:
                if hit in reported:
                    return
                reported.add(hit)
            host, name = hit
            logger.warning(
                "%s deleted the %r cookie in its reply: %s", host, name, SESSION_LOGGED_OUT_MARKER
            )

    @functools.wraps(original)
    def _init_session(self: Any) -> None:
        original(self)
        self.session.hooks["response"].append(_hook)

    Extractor._init_session = _init_session

    def _uninstall() -> None:
        global _INSTALLED
        if _INSTALLED is None:
            return
        Extractor._init_session = original
        _INSTALLED = None

    _INSTALLED = _uninstall
    return _uninstall


def uninstall() -> None:
    """Undo ``install``. Idempotent."""
    if _INSTALLED is not None:
        _INSTALLED()
