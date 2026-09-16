"""Structured event emission for the gallery-dl worker.

The worker writes one JSON object per line to stdout (flushed immediately). The backend's
JobManager reads these lines and forwards them over SSE to the frontend. This module is the
single source of truth for the wire format — see ``docs/event-contract.md``.
"""

import json
import sys
import threading
from typing import Any

# gallery-dl ``status`` bitmask bits (see gallery_dl/job.py).
STATUS_SUCCESS = 0
STATUS_ERROR = 1
STATUS_DOWNLOAD_FAILED = 4
STATUS_SKIPPED = 8
# AuthenticationError / AuthorizationError / AuthRequired all carry code 16. Raised only from
# genuine auth walls in the two extractors this app drives (facebook.py:260,363 and
# instagram.py:1013), so it maps straight onto the reason the contract already has.
STATUS_AUTH = 16
STATUS_NO_EXTRACTOR = 64
STATUS_OS_ERROR = 128

# The heartbeat runs on its own thread while gallery-dl emits from the main thread, so writes must
# be serialized — a half-written line would be unparseable JSON on the manager's side.
_WRITE_LOCK = threading.Lock()


def emit(event: dict[str, Any]) -> None:
    """Write one event as a JSON line to stdout and flush. Thread-safe.

    ``default=str`` is a safety net: gallery-dl metadata can contain non-JSON objects (e.g. a
    PathFormat), which would otherwise raise and kill the event. Coerce them to str.
    """
    line = json.dumps(event, default=str, separators=(",", ":")) + "\n"
    with _WRITE_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


def map_exit_status(status: int) -> tuple[str, str]:
    """Map a gallery-dl bitmask status to (terminal_event_type, reason).

    Priority is ``64 > 128 > 16 > 4 > 1 > 8``: no-extractor / os-error dominate, then auth, then
    download-failed, then generic error; a pure ``8`` (all-skipped) is treated as success.

    ``16`` is mapped rather than falling into ``unknown-16``: it is the one bit whose meaning the
    operator can act on directly. It has to sit **above** ``4``, not below — ``Extractor.status``
    accumulates ``4`` from any fatal ``HttpError``/``NotFoundError`` earlier in the run
    (``extractor/common.py:219,265``) and ``Job.run``'s ``finally`` ORs that in, so ``4 | 16`` is
    the ordinary case and a lower placement would almost never fire. This is a backstop: when the
    stderr tail survives, ``_annotate_failure`` reaches the same reason from the message text.
    """
    if status == STATUS_SUCCESS:
        return "completed", "ok"
    if status & STATUS_NO_EXTRACTOR:
        return "failed", "no-extractor"
    if status & STATUS_OS_ERROR:
        return "failed", "os-error"
    if status & STATUS_AUTH:
        return "failed", "login-required"
    if status & STATUS_DOWNLOAD_FAILED:
        return "failed", "dl-failed"
    if status & STATUS_ERROR:
        return "failed", "error"
    if status & STATUS_SKIPPED:
        return "completed", "all-skipped"
    return "failed", f"unknown-{status}"
