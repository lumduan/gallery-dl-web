"""Adaptive per-request pacing for the gallery-dl worker.

**Why this exists.** gallery-dl paces extractor requests with a single ``sleep-request`` value,
sampled per request and fixed for the run. That is the wrong shape for Facebook: it fetches one
full HTML page *per photo* (``facebook.py:extract_set`` walks a singly-linked list of photo ids),
so the delay is paid once per image, where Instagram amortizes it over ~30 posts per JSON page.
A fixed delay therefore has to be set for the worst case and makes the common case slow.

**What it does instead.** ``AdaptivePacer`` starts at the floor and only slows down when the
platform actually pushes back — a 429, a 403/503, a login redirect, Facebook's block page, or a
request that failed outright. Recovery is gradual: after ``decay_after`` consecutive clean
responses the delay is divided by ``growth`` again, down to the floor.

**The volume ramp is the other half, and it is the one that matters.** The only block this project
has actually observed came after ~767 images in a single Facebook run; short runs never trip it.
So the *floor* itself rises with the number of requests already made, which keeps small jobs at
full speed and makes long jobs progressively more polite without any signal being needed.

**How it is wired in.** The pacer never sleeps. It hands gallery-dl a zero-argument callable and
lets ``Extractor.request`` do the waiting, because gallery-dl already credits time spent doing
work against the interval (``seconds = interval - (now - request_timestamp)``, ``common.py``) —
so the delay is a floor on request *spacing*, not an addend. With a 1 s floor and a 1-2 s page
fetch the added sleep is frequently zero, which is the point: the run goes as fast as the work
allows and the ramp, not the floor, is what protects a long one.

``install()`` patches at *class* level, which is what makes it cover the child jobs a profile
extraction spawns; a per-instance assignment would silently miss them, exactly as
``Job.register_hooks`` does for the progress callbacks. Three pieces:

* a wrapper on ``Extractor.request`` — injects the delay callable and classifies raised errors;
* a ``requests`` **response hook** (installed by wrapping ``Extractor._init_session``) — this is
  where responses are judged, because it is the only place that sees *intermediate retries*,
  *redirect hops*, and **image downloads**, which go straight to ``extractor.session`` and would
  otherwise hide every CDN 429 from us;
* a root ``logging.Handler`` — because **on Facebook a rate limit is an HTTP 200.** A soft block
  is a photo page whose image URL will not parse, and the only in-band evidence is gallery-dl's
  own ``"Failed to find photo download URL"`` warning. Status codes alone are blind to it.

**The hook sees two populations, and only one of them is evidence of health.** Media downloads
share ``extractor.session``, so they arrive here too — but they bypass ``Extractor.request``, so
they are neither paced nor counted toward the ramp, and on Instagram they outnumber extractor
requests ~30:1. Counting them as clean decayed a ceiling-level penalty back to the floor inside a
single page, which made the controller unable to hold *any* back-off. ``observe`` therefore judges
their status but withholds ``clean()`` — see its docstring.

Every observation is also recorded to a bounded ring buffer (``telemetry.RequestLog``) that the
worker flushes into its terminal event, so a run that ends in a block can be diagnosed from what
the platform actually sent rather than from a guess.

Pure logic plus those three patches — no I/O of its own beyond the injected ``emit`` callback, so
the whole thing is unit-testable without a network.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from gallery_dl_web.gallerydl import telemetry
from gallery_dl_web.gallerydl.errors import detect_rate_limit

# A payload can ask for any ceiling; this is the one it can never exceed. Above roughly this, a
# job that is legitimately backed off starts tripping the manager's progress deadline — and the
# binding case is not steady state but the Facebook fallback stall (see ``_FB_DEFAULTS``):
# ``(fallback_retries + 1) * delay + fallback_retries * sleep_429`` has to stay well under
# ``stall_floor_seconds``. The manager clamps again, against the *configured* stall settings; this
# constant is what protects a hand-written payload passed to the worker directly.
HARD_MAX_DELAY = 60.0

# Event budget. ``JobState.events`` is a ``deque(maxlen=5000)`` and ``media_paths()`` /
# ``downloaded_paths()`` derive the zip contents and the profile reconcile by scanning it — so a
# chatty event type silently evicts `file` events and truncates a job's downloads.
MIN_EVENT_INTERVAL = 5.0
MAX_EVENTS = 200
# Smallest delay change worth telling the operator about.
EVENT_MIN_CHANGE = 0.25

# Only ever scan the front of a body; a Facebook page is 1-3 MB and the marker is not at the end
# of a 40 MB video either.
BODY_SCAN_LIMIT = 65536

# gallery-dl's ``util.NullResponse`` status: ``Extractor.request`` returns one instead of raising
# when ``fatal`` is falsy and every retry was used up. It is a failure, not a clean response.
#
# ⚠️ INTENTIONALLY INERT via ``observe``. gallery-dl *constructs* a NullResponse itself
# (``common.py:258``) and returns it straight out of ``Extractor.request``, so it never traverses
# ``Session.send`` and the response hook — ``observe``'s only caller — never receives one. Nothing
# is lost: the real 429/5xx responses that preceded it were each already observed on their own way
# through the hook, so penalising the NullResponse too would double-count them. Kept because
# ``observe`` is public and unit-tested directly, and because removing it is a behaviour question
# rather than a documentation one.
_NULL_RESPONSE_STATUS = 900

# Statuses that mean "the platform is pushing back", as opposed to "this URL is wrong".
# 404 is deliberately absent — a missing photo says nothing about our request rate.
_HARD_PENALTY_STATUS = frozenset({429})
_SOFT_PENALTY_STATUS = frozenset({403, 503, _NULL_RESPONSE_STATUS})

# Facebook's block page, byte-for-byte as gallery-dl itself matches it in
# ``facebook.py:photo_page_request_wrapper`` right before raising AbortExtraction. Matching the
# same bytes lets us report the back-off one step earlier than the traceback.
# The *prose* gallery-dl then logs ("temporarily blocked from viewing images") is what
# ``errors.py:_RATE_LIMIT_PATTERNS`` matches on stderr — two views of one event.
_BLOCK_MARKERS: tuple[bytes, ...] = (
    b'{"__dr":"CometErrorRoot.react"}',
    b"temporarily blocked",
)

# A redirect to any of these means the session is gone or the content is walled.
_LOGIN_PATH_MARKERS: tuple[str, ...] = (
    "facebook.com/login",
    "instagram.com/accounts/login",
)

# Facebook's soft block, which never shows up as a status code: the photo page comes back 200 and
# simply has no parseable image URL, and gallery-dl logs this before sleeping `sleep-429` and
# retrying. See ``facebook.py:extract_set``.
_LOG_SOFT_PENALTY = ("failed to find photo download url",)

# Live equivalents of the challenge wording ``util.detect_challenge`` logs. A challenge means stop
# now, not slow down a little.
_LOG_HARD_PENALTY = ("cloudflare challenge", "cloudflare captcha", "ddos-guard")


def _buffered_body(response: Any) -> bytes:
    """Bytes already in memory for this response, or ``b""``. Never triggers a read.

    Touching ``response.content`` is not safe here. The sensor runs inside a ``requests`` response
    hook, and ``Session.send`` dispatches hooks *before* its own ``if not stream: r.content``
    preload — so the body is unbuffered even for a non-streamed request. On a streamed one
    (every image download) reading it does not raise either: it silently buffers the whole file
    into RAM, which no test would notice.

    ``_content_consumed is True and _content is bytes`` is exactly "already buffered": an
    iter_content-exhausted response sets the flag but leaves ``_content`` as ``False``, and
    ``util.NullResponse`` has neither attribute.
    """
    if getattr(response, "_content_consumed", False) is not True:
        return b""
    body = getattr(response, "_content", None)
    if not isinstance(body, bytes | bytearray):
        return b""
    return bytes(body[:BODY_SCAN_LIMIT])


class AdaptivePacer:
    """Tracks one run's inter-request delay and adjusts it from observed responses.

    ``min_delay`` is the floor (and the starting delay); ``max_delay`` the back-off ceiling.
    Everything else has a default tuned in ``config.py`` and is exposed only so tests and the
    operator can move it.
    """

    def __init__(
        self,
        min_delay: float,
        max_delay: float,
        *,
        growth: float = 3.0,
        decay_after: int = 10,
        # The ramp does the heavy lifting, so it is tuned to land a long Facebook run *above* the
        # old fixed 3-8 s range while leaving a short one essentially unpaced: from a 1 s floor it
        # reaches ~2 s at 200 requests, ~4.8 s at the ~767 images that produced the one observed
        # block, and its 8 s ceiling at ~1400.
        ramp_scale: float = 200.0,
        ramp_max: float = 8.0,
        emit: Callable[[dict[str, Any]], None] | None = None,
        platform: str = "",
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_delay = max(0.0, float(min_delay))
        self.max_delay = min(HARD_MAX_DELAY, max(self.min_delay, float(max_delay)))
        # Below 1.0 the "back-off" would shrink the delay and the decay would grow it.
        self.growth = max(1.01, float(growth))
        self.decay_after = max(1, int(decay_after))
        self.ramp_scale = max(1.0, float(ramp_scale))
        self.ramp_max = max(1.0, float(ramp_max))
        self._emit = emit
        self._platform = platform
        self._now = now

        # The response hook and the log handler can both fire off the main thread.
        self._lock = threading.RLock()
        self._log = telemetry.RequestLog()
        self._delay = self.min_delay
        self._clean = 0
        self._requests = 0
        self._enabled = True
        self._events = 0
        self._last_event = 0.0
        self._reported = self._delay

    # -- state -------------------------------------------------------------------------------

    @property
    def delay(self) -> float:
        """The delay that would be used for the next request."""
        return self._delay

    @property
    def requests(self) -> int:
        return self._requests

    def disable(self) -> None:
        """Stop pacing. ``next_delay`` then returns 0.

        ``uninstall`` calls this because the wrapper binds ``next_delay`` onto each *extractor
        instance* and nothing un-binds it; a live extractor outliving the patch would otherwise
        keep sleeping on a controller nobody is feeding any more.
        """
        with self._lock:
            self._enabled = False

    @property
    def floor(self) -> float:
        """The current floor, raised by the volume ramp.

        Long runs are the ones that get blocked (the observed case was ~767 images), so the floor
        climbs with cumulative requests instead of waiting for a signal that may never come until
        it is too late to matter.
        """
        ramp = min(self.ramp_max, 1.0 + self._requests / self.ramp_scale)
        return min(self.max_delay, self.min_delay * ramp)

    # -- the callable handed to gallery-dl ---------------------------------------------------

    def begin_request(self) -> None:
        """Count one outgoing request and re-apply the (possibly risen) ramp floor.

        Called from the ``Extractor.request`` wrapper rather than from ``next_delay``, because
        gallery-dl only consults ``_interval_request`` when the call opted into pacing
        (``interval=True``). ``request_location``'s HEAD probes do not — but the platform still
        sees them, so they still count toward the ramp.
        """
        with self._lock:
            self._requests += 1
            self._apply(max(self._delay, self.floor), "ramp")

    def next_delay(self) -> float:
        """Zero-arg float callable, shaped exactly like ``util.build_duration_func``'s result.

        A **pure read**. ``Extractor.request``'s retry loop calls it a second time per retry to
        floor its own backoff (``common.py``: ``seconds = max(retry, request, 429)``), so a getter
        that advanced the model would corrupt it on every retried request.
        """
        return self._delay if self._enabled else 0.0

    # -- signals -----------------------------------------------------------------------------

    def observe(self, response: Any, *, streamed: bool = False) -> None:
        """Feed one response to the controller. Called once per adapter send.

        **A streamed response never advances the clean streak**, and that asymmetry is the whole
        point. Media downloads share ``extractor.session`` (``downloader/common.py:29``) so they
        reach this hook, but they bypass ``Extractor.request`` — so they are neither paced nor
        counted toward the volume ramp, while arriving ~30x more often than an extractor request on
        Instagram (~30 images per JSON page). Letting them count as evidence of health meant a hard
        penalty decayed from the ceiling back to the floor within ~20 downloads, i.e. *inside a
        single page*: the controller could not hold an elevated delay at all, whatever it detected.
        Their **status is still judged** — a CDN 429 on an image is real pushback and must still
        penalise. Only the "looked fine, so speed up" conclusion is withheld.
        """
        reason = self._classify(response, streamed=streamed)
        self._record(response, streamed=streamed, reason=reason)
        if reason is None:
            if not streamed:
                self.clean()
        elif reason in ("http-429", "block-page", "login-redirect"):
            self.penalize(reason, hard=True)
        else:
            self.penalize(reason)

    def _record(self, response: Any, *, streamed: bool, reason: str | None) -> None:
        """Append one telemetry entry describing what the controller just saw."""
        with self._lock:
            delay, floor, ceiling = self._delay, self.floor, self.max_delay
        self._log.record(
            response,
            streamed=streamed,
            delay=delay,
            floor=floor,
            ceiling=ceiling,
            classified=reason or "clean",
        )

    def telemetry(self) -> list[dict[str, Any]]:
        """The last ``telemetry.LOG_SIZE`` requests, oldest first. Safe to embed in an event."""
        return self._log.snapshot()

    def observe_exception(self, exc: BaseException) -> None:
        """Classify an error raised out of ``Extractor.request``.

        A 404 is not congestion — ``notfound=`` turns a missing photo into ``NotFoundError``, and
        backing off for it would punish a healthy run for someone else's deleted post.
        """
        name = type(exc).__name__
        if name == "NotFoundError":
            return
        challenge = name == "ChallengeError"
        self.penalize("challenge" if challenge else "request-error", hard=challenge)

    def observe_log(self, level: int, message: str) -> None:
        """Classify one gallery-dl log record. The only Facebook soft-block sensor there is."""
        if level < logging.WARNING:
            return
        lowered = message.lower()
        if any(m in lowered for m in _LOG_HARD_PENALTY):
            self.penalize("challenge", hard=True)
            return
        # Reuses the very patterns the post-mortem stderr classifier uses, so the live sensor and
        # the reported failure reason can never drift apart.
        if detect_rate_limit([message]) is not None:
            self.penalize("rate-limited", hard=True)
            return
        if any(m in lowered for m in _LOG_SOFT_PENALTY):
            self.penalize("no-download-url")

    def clean(self) -> None:
        with self._lock:
            self._clean += 1
            if self._clean >= self.decay_after:
                self._clean = 0
                self._apply(max(self.floor, self._delay / self.growth), "recovered")

    def penalize(self, reason: str, *, hard: bool = False) -> None:
        """Slow down. ``hard`` (a 429 or an outright block) goes straight to the ceiling."""
        with self._lock:
            self._clean = 0
            target = self.max_delay if hard else min(self.max_delay, self._delay * self.growth)
            self._apply(max(target, self.floor), reason)

    # -- internals ---------------------------------------------------------------------------

    def _classify(self, response: Any, *, streamed: bool) -> str | None:
        """The penalty reason for this response, or None if it looked healthy."""
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            if status in _HARD_PENALTY_STATUS:
                return "http-429"
            if status in _SOFT_PENALTY_STATUS:
                return f"http-{status}"

        url = getattr(response, "url", None)
        if isinstance(url, str) and any(m in url for m in _LOGIN_PATH_MARKERS):
            return "login-redirect"

        body = b"" if streamed else _buffered_body(response)
        if body and any(m in body for m in _BLOCK_MARKERS):
            return "block-page"
        return None

    def _apply(self, value: float, reason: str) -> None:
        new = min(self.max_delay, max(0.0, value))
        self._delay = new
        # Compared against the last *reported* delay, not the last one: the ramp moves in steps of
        # a few milliseconds, so comparing consecutive values would let it climb from 1 s to 8 s
        # without ever emitting anything.
        if abs(new - self._reported) < EVENT_MIN_CHANGE:
            return
        if self._emit is None or not self._should_emit():
            return
        # `previous` is the last value we *reported*, so the pair always describes the change the
        # operator is actually being shown.
        previous, self._reported = self._reported, new
        self._emit(
            {
                "type": "pacing",
                "platform": self._platform,
                "delay": round(new, 2),
                "previous": round(previous, 2),
                "reason": reason,
                "requests": self._requests,
            }
        )

    def _should_emit(self) -> bool:
        """Rate-limit our own events.

        ``JobState.events`` is a bounded deque that the zip route and the profile reconcile read
        `file` events back out of, so an unbounded event type does not merely spam the UI — it
        evicts the record of which files a job fetched.
        """
        if self._events >= MAX_EVENTS:
            return False
        now = self._now()
        if self._events and now - self._last_event < MIN_EVENT_INTERVAL:
            return False
        self._events += 1
        self._last_event = now
        return True


# --- the log sensor ------------------------------------------------------------------------------


class PacingLogHandler(logging.Handler):
    """Feeds gallery-dl's own warnings to the pacer.

    ``Extractor.log`` is ``logging.getLogger(self.category)`` and the worker's
    ``output.initialize_logging`` sets the root logger to ``NOTSET``, so every extractor warning
    reaches a root handler — which is how one handler covers both platforms and every child job
    without knowing any logger names. Level WARNING is not cosmetic: at NOTSET the root sees
    urllib3's DEBUG line for every single request.
    """

    def __init__(self, pacer: AdaptivePacer) -> None:
        super().__init__(level=logging.WARNING)
        self._pacer = pacer

    def emit(self, record: logging.LogRecord) -> None:
        # A raising handler would surface as a logging error mid-download; nothing here is worth
        # that. (logging.Handler.handleError already swallows, but getMessage() is outside it.)
        with contextlib.suppress(Exception):
            self._pacer.observe_log(record.levelno, record.getMessage())


# --- the monkeypatch -----------------------------------------------------------------------------

_INSTALLED: Any = None


def install(pacer: AdaptivePacer) -> Callable[[], None]:
    """Route gallery-dl's requests through ``pacer``. Idempotent; returns an uninstall callable.

    Patching the classes, not an instance, is load-bearing: Facebook/Instagram profile extraction
    spawns child jobs with their own extractor objects, and the run's request budget is shared,
    not per-extractor.
    """
    global _INSTALLED
    if _INSTALLED is not None:
        return _INSTALLED  # type: ignore[no-any-return]

    from gallery_dl.extractor.common import Extractor

    original_request = Extractor.request
    original_init_session = Extractor._init_session
    handler = PacingLogHandler(pacer)
    root = logging.getLogger()

    def _hook(response: Any, *_args: Any, **kwargs: Any) -> None:
        # requests dispatches this once per adapter send — so once per *retry* and once per
        # redirect hop, which is why judging the return value of Extractor.request instead would
        # score `429, 429, 200` as a single clean response. It also catches image downloads, which
        # go straight to extractor.session and would otherwise hide every CDN 429 from us.
        # Returning anything non-None would REPLACE the response, and raising escapes Session.send.
        with contextlib.suppress(Exception):
            pacer.observe(response, streamed=bool(kwargs.get("stream")))

    def _init_session(self: Any) -> None:
        original_init_session(self)
        self.session.hooks["response"].append(_hook)

    @functools.wraps(original_request)
    def _paced(self: Any, url: str, *args: Any, **kwargs: Any) -> Any:
        # Re-assigned per call because gallery-dl's ``_init_options`` sets this per extractor
        # instance, and child extractors are constructed long after install() ran.
        self._interval_request = pacer.next_delay
        pacer.begin_request()
        try:
            return original_request(self, url, *args, **kwargs)
        except Exception as exc:
            pacer.observe_exception(exc)
            raise

    Extractor.request = _paced
    Extractor._init_session = _init_session
    root.addHandler(handler)

    def _uninstall() -> None:
        global _INSTALLED
        if _INSTALLED is None:
            return
        Extractor.request = original_request
        Extractor._init_session = original_init_session
        with contextlib.suppress(Exception):
            root.removeHandler(handler)
        # Extractors still alive hold a bound `next_delay`; nothing un-binds it, so make it inert.
        pacer.disable()
        _INSTALLED = None

    _INSTALLED = _uninstall
    return _uninstall


def uninstall() -> None:
    """Undo ``install``. Idempotent."""
    if _INSTALLED is not None:
        _INSTALLED()
