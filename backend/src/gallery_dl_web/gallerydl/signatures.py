"""What platform pushback actually looks like, as data rather than conditionals.

**Every signature here that is marked OBSERVED was captured from a real blocked run**, not inferred
from a platform's documentation or from gallery-dl's source. That distinction is the whole point of
this module: the previous generation of this logic was written from plausible-looking wording and
missed the failure that actually happens. ``CLAUDE.md`` records the same lesson for the login-wall
classifier — it matched gallery-dl's ``AuthRequired`` prose, while the real Instagram failure was a
urllib3 debug line with the word "Unauthorized" nowhere on it.

**The Instagram block is a 302 to the home page.** Captured 2026-08-26, after 859 downloads and
885 s at ``adaptive 4-30``::

    i=937  302  www.instagram.com/api/v1/clips/user/   text/html   body=""
    i=938  200  www.instagram.com/                     text/html   body="<!DOCTYPE html>…"
    -> gallery_dl.exception.AbortExtraction: HTTP redirect to home page

It is **not** an HTTP 200 carrying ``{"status": "fail"}``, which is what the original bug report
hypothesised, and it is not a 429. All 50 requests in that run's ring buffer classified ``clean``,
including the redirect that killed it, because the only URL markers were ``/accounts/login`` and
Facebook's — and the redirect target is the bare domain root.

**Tiers, and why the split matters.** ``THROTTLE`` is transient: back off toward the ceiling and
keep going. ``TERMINAL`` means stop — retrying into a checkpoint extends the block and burns the
session, and gallery-dl has already raised ``AbortExtraction`` by the time we see it, so backing off
achieves nothing. ``UNKNOWN`` is the one that used to be missing: an unclassifiable response must
**not** feed the clean streak, because "we could not tell" is not evidence of health.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit


class Tier(StrEnum):
    """What the controller should do about one observed response."""

    THROTTLE = "throttle"  # transient: back off, keep running
    TERMINAL = "terminal"  # stop; retrying makes it worse
    CLEAN = "clean"  # genuine success; may advance the clean streak
    UNKNOWN = "unknown"  # unclassifiable; neutral, never clean


@dataclass(frozen=True)
class Observation:
    """One response, reduced to what a signature is allowed to look at.

    ``url`` is the **raw** URL here, unlike ``telemetry``'s host+path form: matching happens
    in-process and never leaves it, while the telemetry record is built for sharing.
    """

    status: int | None
    url: str
    content_type: str
    body: str
    streamed: bool

    @property
    def host(self) -> str:
        try:
            return (urlsplit(self.url).hostname or "").lower()
        except ValueError:
            return ""

    @property
    def path(self) -> str:
        try:
            return urlsplit(self.url).path or ""
        except ValueError:
            return ""

    def body_has(self, *needles: str) -> bool:
        lowered = self.body.lower()
        return any(n in lowered for n in needles)


@dataclass(frozen=True)
class Signature:
    """A named rule. ``name`` is what reaches the operator, so make it diagnostic."""

    name: str
    tier: Tier
    match: Callable[[Observation], bool]


# --- Instagram -----------------------------------------------------------------------------------


def _is_meta_host(obs: Observation) -> bool:
    return obs.host.endswith("instagram.com")


def _redirected_to_root(obs: Observation) -> bool:
    """The observed block: an API request that lands on the bare domain root.

    Matches both halves of the bounce — the 302 whose body is empty and the 200 that serves the
    homepage — because the response hook sees them as two separate responses and either one is
    enough to know we have been thrown out.
    """
    return _is_meta_host(obs) and obs.path in ("", "/")


def _api_path(obs: Observation) -> bool:
    return obs.path.startswith("/api/") or "/graphql" in obs.path


def _html_for_api(obs: Observation) -> bool:
    """HTML served where JSON was asked for. Anomalous, but not self-describing."""
    return _is_meta_host(obs) and _api_path(obs) and "html" in obs.content_type.lower()


INSTAGRAM: tuple[Signature, ...] = (
    # OBSERVED 2026-08-26. The real block. gallery-dl raises AbortExtraction on it
    # (instagram.py:186), so by the time we classify, the run is already over -- the value is in
    # reporting it as a rate limit rather than a traceback, and in never treating it as clean.
    Signature("ig-redirect-root", Tier.TERMINAL, _redirected_to_root),
    # gallery-dl detects these two itself (instagram.py:178-181) and aborts. Not seen in the
    # capture, but they share the redirect mechanism above and cost nothing to carry.
    Signature(
        "ig-redirect-login",
        Tier.TERMINAL,
        lambda o: _is_meta_host(o) and "/accounts/login" in o.path,
    ),
    Signature(
        "ig-redirect-challenge",
        Tier.TERMINAL,
        lambda o: _is_meta_host(o) and "/challenge" in o.path,
    ),
    # NOT observed in the capture. Carried because errors.py already matches the same wording on
    # stderr, where it came from real operator reports -- so the two sensors cannot drift apart.
    Signature(
        "ig-account-flagged",
        Tier.TERMINAL,
        lambda o: o.body_has("checkpoint_required", "challenge_required", "feedback_required"),
    ),
    Signature(
        "ig-wait-message",
        Tier.THROTTLE,
        lambda o: o.body_has("please wait a few minutes before you try again"),
    ),
    # Weaker than the redirect rules and deliberately below them: HTML on an API path says
    # something is wrong without saying what, so it slows down rather than stopping.
    Signature("ig-html-for-api", Tier.THROTTLE, _html_for_api),
)


# --- Facebook ------------------------------------------------------------------------------------

# Byte-for-byte what facebook.py:261 matches before raising AbortExtraction. Verified still present
# in gallery-dl 1.32.9.
_FB_BLOCK_MARKERS = ('{"__dr":"cometerrorroot.react"}', "temporarily blocked")

FACEBOOK: tuple[Signature, ...] = (
    Signature(
        "fb-block-page",
        Tier.TERMINAL,
        lambda o: o.body_has(*_FB_BLOCK_MARKERS),
    ),
    Signature(
        "fb-redirect-login",
        Tier.TERMINAL,
        lambda o: o.host.endswith("facebook.com") and "/login" in o.path,
    ),
)


# --- shared --------------------------------------------------------------------------------------

# Statuses that mean "the platform is pushing back", as opposed to "this URL is wrong". 404 is
# deliberately absent: a missing photo says nothing about our request rate.
# 900 is gallery-dl's ``util.NullResponse``, returned instead of raising when ``fatal`` is falsy
# and every retry is used up. It cannot reach the response hook -- gallery-dl constructs it itself
# (``common.py:258``) and returns it straight out of ``Extractor.request``, so it never traverses
# ``Session.send`` -- but ``classify`` is public and unit-tested directly, and a failure is a
# failure however it arrives.
_STATUS_TIERS: tuple[tuple[frozenset[int], Tier, str], ...] = (
    (frozenset({429}), Tier.THROTTLE, "http-429"),
    (frozenset({403, 503, 900}), Tier.THROTTLE, "http-{status}"),
)

PLATFORMS: dict[str, tuple[Signature, ...]] = {
    "instagram": INSTAGRAM,
    "facebook": FACEBOOK,
}

_ALL_SIGNATURES: tuple[Signature, ...] = INSTAGRAM + FACEBOOK


def classify(platform: str, obs: Observation) -> tuple[Tier, str | None]:
    """``(tier, rule name)`` for one response. Never raises.

    Order is load-bearing. Status codes are checked first because they are unambiguous, and
    platform rules before the generic fallbacks so a Facebook block page is reported as such rather
    than as "html where json was expected".
    """
    for statuses, tier, template in _STATUS_TIERS:
        if obs.status in statuses:
            return tier, template.format(status=obs.status)

    # An unrecognised platform runs EVERY table rather than none. Failing toward detection is the
    # right default here, and it is safe: the redirect rules are host-scoped and the body markers
    # are platform-specific strings, so a cross-platform false positive is not reachable.
    tables = PLATFORMS.get(platform)
    for signature in tables if tables is not None else _ALL_SIGNATURES:
        try:
            if signature.match(obs):
                return signature.tier, signature.name
        except Exception:  # noqa: BLE001 — a broken rule must not fail the download
            continue

    # A body we were entitled to read and could not make sense of. NOT clean: "we could not tell"
    # is not evidence of health, and treating it as such is half of what made the old controller
    # unable to react at all.
    if (
        not obs.streamed
        and obs.body
        and "json" in obs.content_type.lower()
        and not obs.body.lstrip().startswith(("{", "["))
    ):
        return Tier.UNKNOWN, "unparseable-json"

    return Tier.CLEAN, None
