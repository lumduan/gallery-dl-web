"""Classify worker stderr into actionable failure reasons.

gallery-dl reports *what* failed through its exit-status bitmask (see ``events.map_exit_status``),
but the *why* only ever reaches its stderr log. Platform rate-limiting is the case worth pulling
out: it is not a defect, retrying makes it worse, and the operator's only useful action is to wait.
Left unclassified it surfaces as a bare ``dl-failed`` plus a Python traceback, which reads like an
application bug.

Pure text in, structured verdict out — no I/O, so it is cheap to unit-test against real log output.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# Matched case-insensitively against each stderr line. Each entry is (pattern, operator-facing
# explanation). Keep the platform's own wording in the explanation where it is already clear.
_RATE_LIMIT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"temporarily blocked from viewing images", re.I),
        "Facebook has temporarily blocked this account from viewing images. This is a platform "
        "rate limit, not a download error — retrying now usually extends it. Wait (hours, "
        "sometimes a day), then run the profile again; files already downloaded are skipped.",
    ),
    (
        re.compile(r"please wait a few minutes before you try again", re.I),
        "Instagram is rate-limiting this account ('please wait a few minutes before you try "
        "again'). Wait before retrying; files already downloaded are skipped.",
    ),
    (
        # A bare "429" is far too loose — it matches file counts and media ids. Require either the
        # phrase itself or 429 in an HTTP-ish context.
        re.compile(r"too many requests|\b(?:http|https|status|code|error)\W{0,3}429\b", re.I),
        "The platform returned HTTP 429 (too many requests). Wait before retrying, and consider "
        "raising the sleep-request range for this platform.",
    ),
    (
        re.compile(r"\bchallenge_required\b|\bcheckpoint_required\b", re.I),
        "The platform is asking this account to complete a security challenge. Log into the site "
        "in a browser, clear the checkpoint, then refresh your cookies in Settings.",
    ),
)

# gallery-dl appends "&setextract" to a URL you can resume a Facebook set from.
_RESUME_URL_RE = re.compile(r"https?://\S*setextract\S*")


@dataclass(frozen=True)
class RateLimit:
    """A detected platform rate limit / block."""

    message: str
    resume_url: str | None = None


def detect_rate_limit(stderr_lines: Iterable[str]) -> RateLimit | None:
    """Return a ``RateLimit`` if the worker's stderr shows a platform block, else None."""
    lines = list(stderr_lines)
    hit: str | None = None
    for line in lines:
        for pattern, explanation in _RATE_LIMIT_PATTERNS:
            if pattern.search(line):
                hit = explanation
                break
        if hit:
            break
    if hit is None:
        return None

    resume: str | None = None
    for line in lines:
        match = _RESUME_URL_RE.search(line)
        if match:
            # Trailing punctuation from log formatting would break the link.
            resume = match.group(0).rstrip(").,;'\"")
            break
    return RateLimit(message=hit, resume_url=resume)
