"""Per-request evidence capture for the adaptive pacer.

**Why this exists.** The pacer decides from ``(status_code, url)`` and, in principle, a response
body — but it has never actually seen a body, so its Instagram behaviour could only be guessed at.
``pacing._buffered_body`` refuses to trigger a read and returns bytes only when
``_content_consumed is True``; at the moment a ``requests`` response hook runs, that flag is always
``False``. ``Session.send`` dispatches hooks at ``sessions.py:791`` and buffers the body at
``sessions.py:827`` — 36 lines later. So the body branch of the classifier is unreachable in
production, and nobody could tell, because ``FakeResponse`` in the tests defaulted the flag to
``True``.

This module makes the body observable *safely*, and records what the controller saw so a blocked
run can be diagnosed without reproducing it.

**Reading the body is safe for a non-streamed response, and only for one.** ``requests`` performs
the identical read itself immediately afterwards, and ``Response.content`` memoises into
``_content`` — so reading early costs no extra memory and changes no behaviour. On a *streamed*
response (every media download, ``downloader/http.py:164`` passes ``stream=True``) it would
silently buffer a whole 40 MB video into RAM, which no assertion would notice. The ``stream``
keyword forwarded to the hook is the only reliable discriminator: ``response.raw.closed`` is
``False`` for streamed and non-streamed alike at hook time, so it cannot tell them apart.

**Redaction is allowlist-first.** The URL keeps only host + path — the query is dropped whole,
because Instagram's media URLs carry signed tokens there. Exactly two response headers are read,
and no *request* header is ever touched, which is where ``Cookie`` and ``Authorization`` live. A
free-form body prefix cannot be allowlisted, so it is truncated and then masked; the guarantee for
that last case is the redaction test, not the mechanism.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from typing import Any
from urllib.parse import urlsplit

# How many requests of history to keep. Enough to cover the run-up to a block without turning the
# terminal event into a payload nobody will read (50 x ~500 B ~= 25 KB).
LOG_SIZE = 50

# How much of a body to keep. A failure envelope announces itself in the first line or two.
BODY_PREFIX_BYTES = 500

# Above this, skip the body entirely. `requests` buffers it regardless for a non-streamed response,
# so this bounds our *scan and payload*, not memory.
MAX_CAPTURE_BYTES = 1_048_576

# The only response headers ever read.
_HEADER_ALLOWLIST = ("content-type", "content-length")

# Bodies are captured only for types a pushback signature could plausibly match. Anything else
# (images, video, octet-stream) is skipped before a single byte is touched.
_CAPTURABLE_TYPES = (
    "application/json",
    "text/json",
    "text/html",
    "application/xhtml",
    "text/plain",
)

# Values of these keys are masked wherever they appear in a captured prefix, in either JSON
# (`"key": "value"`) or query/form (`key=value`) shape.
_SENSITIVE_KEYS = (
    "sessionid",
    "csrftoken",
    "csrf_token",
    "x-csrftoken",
    "ds_user_id",
    "authorization",
    "access_token",
    "oauth_token",
    "password",
    "cookie",
    "token",
)

REDACTED = "<redacted>"

# Group 1 is the key's quoting and group 4 the value's; both are re-emitted so a JSON prefix stays
# valid JSON after masking — the capture is meant to be readable, and a mangled prefix is worth less
# than a redacted one.
_KEY_VALUE_RE = re.compile(
    r'("?)\b(' + "|".join(_SENSITIVE_KEYS) + r')\1\s*([:=])\s*("?)([^"&,}\s]+)\4',
    re.I,
)

# A long unbroken run of token-ish characters is masked even when its key is not recognised. This
# over-masks (it also catches media ids), which is the safe direction: telemetry does not need them
# and a leaked session cookie is unrecoverable. Prose survives it — the wording we care about
# ("Please wait a few minutes before you try again") contains spaces.
_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def _redact(text: str) -> str:
    """Mask credential-shaped content in a captured body prefix."""

    def _mask(m: re.Match[str]) -> str:
        key_q, key, sep, val_q = m.group(1), m.group(2), m.group(3), m.group(4)
        return f"{key_q}{key}{key_q}{sep}{val_q}{REDACTED}{val_q}"

    return _TOKEN_RE.sub(REDACTED, _KEY_VALUE_RE.sub(_mask, text))


def safe_url(url: Any) -> str:
    """Host + path only. Scheme, credentials, query and fragment are dropped, not masked.

    The query is where a signed CDN token lives, so it never enters the record in any form.
    """
    if not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = parts.hostname or ""
    return f"{host}{parts.path}"[:200]


def _header(response: Any, name: str) -> str:
    """One allowlisted response header, or ``""``. Never reads anything else."""
    if name not in _HEADER_ALLOWLIST:  # pragma: no cover - guards against a careless caller
        return ""
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    try:
        value = headers.get(name)
    except Exception:
        return ""
    return str(value) if value is not None else ""


def _readable(response: Any, *, streamed: bool) -> bool:
    """Whether the body can be read without buffering a download into RAM.

    Two safe cases: the response is not streamed (``requests`` is about to buffer it anyway), or it
    is already buffered. Everything else is skipped. Note ``response.raw.closed`` is ``False`` in
    *both* the streamed and non-streamed case at hook time, so it cannot be used to tell them apart.
    """
    if not streamed:
        return True
    return getattr(response, "_content_consumed", False) is True


def capture_body(response: Any, *, streamed: bool) -> tuple[str, str]:
    """``(content_type, redacted body prefix)``. The prefix is ``""`` whenever capture is unsafe."""
    ctype = _header(response, "content-type")
    if not any(t in ctype.lower() for t in _CAPTURABLE_TYPES):
        return ctype, ""
    if not _readable(response, streamed=streamed):
        return ctype, ""
    length = _header(response, "content-length")
    if length.isdigit() and int(length) > MAX_CAPTURE_BYTES:
        return ctype, ""
    try:
        body = response.content
    except Exception:
        return ctype, ""
    if not isinstance(body, bytes | bytearray):
        return ctype, ""
    return ctype, _redact(bytes(body[:BODY_PREFIX_BYTES]).decode("utf-8", "replace"))


class RequestLog:
    """A bounded ring buffer of what the pacer saw, newest last.

    Deliberately **not** an SSE event type: ``JobState.events`` is a bounded deque that
    ``media_paths()`` and the zip route read ``file`` events back out of, so a per-request event
    would evict them and silently truncate a job's downloads. This is flushed once, at the end.
    """

    def __init__(self, size: int = LOG_SIZE) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=size)
        self._lock = threading.Lock()
        self._index = 0

    def record(
        self,
        response: Any,
        *,
        streamed: bool,
        delay: float,
        floor: float,
        ceiling: float,
        classified: str,
        rule: str | None = None,
    ) -> None:
        """Append one entry. Never raises — telemetry must not be able to cost a download."""
        try:
            ctype, body = capture_body(response, streamed=streamed)
            status = getattr(response, "status_code", None)
            entry: dict[str, Any] = {
                "i": self._index,
                "delay": round(float(delay), 3),
                "floor": round(float(floor), 3),
                "ceiling": round(float(ceiling), 3),
                "status": status if isinstance(status, int) else None,
                "url": safe_url(getattr(response, "url", None)),
                "content_type": ctype,
                "body": body,
                "streamed": bool(streamed),
                "classified": classified,
                # Populated once the signature table lands; kept here so the record shape does not
                # change under the operator between releases.
                "rule": rule,
            }
        except Exception:
            return
        with self._lock:
            self._index += 1
            self._entries.append(entry)

    def snapshot(self) -> list[dict[str, Any]]:
        """The entries, oldest first. JSON-native — safe to put straight into an event."""
        with self._lock:
            return list(self._entries)
