"""Adaptive pacing: the AIMD controller, the volume ramp, and the Extractor.request patch.

No network anywhere — the controller is fed hand-built response stand-ins, and the patch test
drives a fake Extractor class rather than a real one.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from gallery_dl_web.gallerydl import pacing


class FakeResponse:
    """A response as the ``requests`` hook sees it.

    ``content`` is a property that raises: the sensor must read the *buffered* bytes via the
    private pair, never trigger a read. A regression has to be a hard failure, not a silent
    whole-file-into-RAM buffer that no assertion would notice.
    """

    def __init__(
        self,
        status: int = 200,
        url: str = "https://x/",
        body: bytes | None = b"ok",
        consumed: bool = True,
    ) -> None:
        self.status_code = status
        self.reason = "Fake"
        self.url = url
        self._content_consumed = consumed
        self._content: Any = b"" if body is None else body

    @property
    def content(self) -> bytes:
        raise AssertionError("the pacer must never read .content")


def _clock() -> Any:
    """A monotonic stand-in that always clears MIN_EVENT_INTERVAL, so emit-on-change is testable
    without sleeping."""
    ticks = iter(range(0, 10**6, int(pacing.MIN_EVENT_INTERVAL) + 1))
    return lambda: float(next(ticks))


def _pacer(**over: Any) -> pacing.AdaptivePacer:
    base: dict[str, Any] = {
        "min_delay": 1.0,
        "max_delay": 30.0,
        "growth": 3.0,
        "decay_after": 10,
        # Off unless a test asks for it, so the ramp cannot perturb the AIMD assertions.
        "ramp_scale": 10**9,
        "ramp_max": 1.0,
        "now": _clock(),
    }
    base.update(over)
    return pacing.AdaptivePacer(**base)


# --- the delay ladder ---------------------------------------------------------------------------


def test_starts_at_the_floor() -> None:
    assert _pacer().delay == 1.0


def test_penalty_multiplies_and_ceiling_clamps() -> None:
    p = _pacer()
    p.penalize("soft")
    assert p.delay == 3.0
    p.penalize("soft")
    assert p.delay == 9.0
    p.penalize("soft")
    assert p.delay == 27.0
    p.penalize("soft")
    assert p.delay == 30.0  # clamped, not 81
    p.penalize("soft")
    assert p.delay == 30.0


def test_hard_penalty_jumps_straight_to_the_ceiling() -> None:
    p = _pacer()
    p.penalize("http-429", hard=True)
    assert p.delay == 30.0


def test_decay_needs_a_full_clean_streak_and_stops_at_the_floor() -> None:
    p = _pacer()
    p.penalize("soft")
    assert p.delay == 3.0
    for _ in range(9):
        p.clean()
    assert p.delay == 3.0  # 9 clean responses is not yet a streak
    p.clean()
    assert p.delay == 1.0
    for _ in range(100):
        p.clean()
    assert p.delay == 1.0  # never below the floor


def test_a_penalty_resets_the_clean_streak() -> None:
    p = _pacer()
    p.penalize("soft")
    for _ in range(9):
        p.clean()
    p.penalize("soft")
    for _ in range(9):
        p.clean()
    assert p.delay == 9.0  # the second streak never completed either


def test_inverted_bounds_collapse_instead_of_inverting() -> None:
    p = pacing.AdaptivePacer(min_delay=9.0, max_delay=2.0)
    assert p.min_delay == 9.0
    assert p.max_delay == 9.0
    assert p.delay == 9.0


def test_growth_below_one_is_rejected() -> None:
    """A growth < 1 would make 'back off' speed up and 'recover' slow down."""
    p = _pacer(growth=0.5)
    p.penalize("soft")
    assert p.delay > 1.0


# --- the volume ramp ----------------------------------------------------------------------------


def test_floor_rises_with_cumulative_requests() -> None:
    p = _pacer(ramp_scale=400.0, ramp_max=4.0)
    assert p.floor == 1.0
    for _ in range(400):
        p.begin_request()
    assert p.floor == pytest.approx(2.0)
    assert p.delay == pytest.approx(2.0)


def test_ramp_is_capped_at_ramp_max() -> None:
    p = _pacer(ramp_scale=400.0, ramp_max=4.0)
    for _ in range(10_000):
        p.begin_request()
    assert p.floor == pytest.approx(4.0)


def test_ramp_never_exceeds_the_ceiling() -> None:
    p = _pacer(min_delay=10.0, max_delay=12.0, ramp_scale=100.0, ramp_max=4.0)
    for _ in range(1000):
        p.begin_request()
    assert p.floor == 12.0


def test_decay_cannot_go_below_the_ramped_floor() -> None:
    p = _pacer(ramp_scale=400.0, ramp_max=4.0)
    for _ in range(400):
        p.begin_request()
    p.penalize("soft")
    for _ in range(1000):
        p.clean()
    assert p.delay == pytest.approx(2.0)  # back to the ramped floor, not the raw min


def test_next_delay_is_a_zero_arg_float_callable() -> None:
    """gallery-dl calls ``self._interval_request()`` with no arguments and does float math on it."""
    p = _pacer()
    value = p.next_delay()
    assert isinstance(value, float)
    assert value == 1.0


# --- signal classification ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (FakeResponse(200), 1.0),
        (FakeResponse(404), 1.0),  # a missing photo says nothing about our request rate
        (FakeResponse(429), 30.0),
        (FakeResponse(403), 3.0),
        (FakeResponse(503), 3.0),
        (FakeResponse(900), 3.0),  # util.NullResponse — retries exhausted
        (FakeResponse(302, url="https://www.facebook.com/login/?next=x"), 30.0),
        (FakeResponse(200, url="https://www.instagram.com/accounts/login/"), 30.0),
        (FakeResponse(200, body=b'x{"__dr":"CometErrorRoot.react"}y'), 30.0),
        (FakeResponse(200, body=b"You have been temporarily blocked"), 30.0),
        # Buffered flag set but body still `False` — an iter_content-exhausted response.
        (FakeResponse(200, body=None, consumed=False), 1.0),
    ],
)
def test_observe_classifies_each_signal(response: FakeResponse, expected: float) -> None:
    p = _pacer()
    p.observe(response)
    assert p.delay == expected


def test_a_streamed_body_is_never_scanned() -> None:
    """An image download is streamed; scanning it would buffer the whole file into RAM."""
    p = _pacer()
    p.observe(FakeResponse(200, body=b'{"__dr":"CometErrorRoot.react"}'), streamed=True)
    assert p.delay == 1.0


def test_a_streamed_429_is_still_seen() -> None:
    """Status is always safe to read, and a CDN 429 on an image download is a real signal."""
    p = _pacer()
    p.observe(FakeResponse(429), streamed=True)
    assert p.delay == 30.0


def test_null_response_is_a_penalty() -> None:
    """``fatal=False`` returns util.NullResponse (status 900) rather than raising."""
    from gallery_dl import util

    p = _pacer()
    p.observe(util.NullResponse("https://x/", "boom"))
    assert p.delay == 3.0


def test_observe_tolerates_a_response_without_the_usual_attributes() -> None:
    p = _pacer()
    p.observe(object())
    assert p.delay == 1.0


# --- exception + log sensors ----------------------------------------------------------------------


def test_not_found_is_not_congestion() -> None:
    """`notfound=` makes a deleted post a NotFoundError; backing off for it punishes a good run."""
    from gallery_dl import exception

    p = _pacer()
    p.observe_exception(exception.NotFoundError("user"))
    assert p.delay == 1.0


def test_challenge_is_a_hard_penalty_and_other_errors_are_soft() -> None:
    from gallery_dl import exception

    p = _pacer()
    p.observe_exception(RuntimeError("connection reset"))
    assert p.delay == 3.0
    p2 = _pacer()
    p2.observe_exception(exception.ChallengeError("Cloudflare challenge", FakeResponse(403)))
    assert p2.delay == 30.0


def test_the_log_sensor_catches_facebooks_http_200_soft_block() -> None:
    """The whole reason the log sensor exists: on Facebook a rate limit is not a status code."""
    p = _pacer()
    p.observe_log(
        logging.WARNING,
        "Failed to find photo download URL for https://www.facebook.com/photo/?fbid=0&set=1. "
        "Retrying in 15 seconds.",
    )
    assert p.delay == 3.0


def test_the_log_sensor_reuses_the_stderr_rate_limit_patterns() -> None:
    p = _pacer()
    p.observe_log(logging.ERROR, "You've been temporarily blocked from viewing images.")
    assert p.delay == 30.0


def test_the_log_sensor_ignores_debug_and_unrelated_records() -> None:
    p = _pacer()
    p.observe_log(logging.DEBUG, "Failed to find photo download URL for x")
    p.observe_log(logging.WARNING, "Directory already exists")
    assert p.delay == 1.0


def test_the_log_handler_never_raises() -> None:
    handler = pacing.PacingLogHandler(_pacer())
    record = logging.LogRecord("facebook", logging.WARNING, __file__, 1, "%d", ("nope",), None)
    handler.emit(record)  # getMessage() raises on the bad format arg; must be swallowed


# --- events -------------------------------------------------------------------------------------


def test_emits_only_when_the_delay_changes() -> None:
    seen: list[dict[str, Any]] = []
    p = _pacer(emit=seen.append, platform="facebook")
    for _ in range(9):
        p.clean()
    assert seen == []  # no change yet
    p.penalize("http-403")
    assert len(seen) == 1
    assert seen[0]["type"] == "pacing"
    assert seen[0]["platform"] == "facebook"
    assert seen[0]["delay"] == 3.0
    assert seen[0]["previous"] == 1.0
    assert seen[0]["reason"] == "http-403"
    p.penalize("http-429", hard=True)
    assert len(seen) == 2
    p.penalize("http-429", hard=True)
    assert len(seen) == 2  # already at the ceiling — nothing changed, nothing emitted


def test_the_gradual_ramp_is_still_reported() -> None:
    """The floor climbs a few ms per request. Comparing consecutive values would let it walk from
    1 s to 8 s without ever emitting, so the operator would never learn why a long run slowed."""
    seen: list[dict[str, Any]] = []
    p = _pacer(emit=seen.append, ramp_scale=200.0, ramp_max=8.0)
    for _ in range(1500):
        p.begin_request()
    assert seen, "the ramp climbed 1s -> 8s in silence"
    assert [e["reason"] for e in seen] == ["ramp"] * len(seen)
    # Each event reports the change since the LAST REPORTED value, so the pairs form a chain.
    assert seen[0]["previous"] == 1.0
    for earlier, later in zip(seen, seen[1:], strict=False):
        assert later["previous"] == earlier["delay"]
    assert seen[-1]["delay"] == 8.0


def test_the_event_budget_is_hard_capped() -> None:
    """`JobState.events` is a bounded deque the zip route reads `file` events out of, so an
    unbounded event type does not just spam the UI — it truncates a job's downloads."""
    seen: list[dict[str, Any]] = []
    p = _pacer(emit=seen.append)
    for _ in range(5000):
        p.penalize("http-429", hard=True)
        p._delay = p._reported = 1.0
    assert len(seen) == pacing.MAX_EVENTS


def test_events_are_spaced_in_time() -> None:
    frozen = _pacer(emit=(seen := []).append, now=lambda: 100.0)
    for _ in range(20):
        frozen.penalize("http-429", hard=True)
        frozen._delay = 1.0
    assert len(seen) == 1  # everything after the first is inside MIN_EVENT_INTERVAL


def test_events_carry_no_url_or_body() -> None:
    """The event contract forbids leaking anything but counts and filenames."""
    seen: list[dict[str, Any]] = []
    p = _pacer(emit=seen.append, platform="facebook")
    p.observe(FakeResponse(429, url="https://www.facebook.com/photo/?fbid=SECRET"))
    assert seen
    assert set(seen[0]) == {"type", "platform", "delay", "previous", "reason", "requests"}


# --- the Extractor.request patch ------------------------------------------------------------------


@pytest.fixture
def fake_extractor(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stand in for gallery_dl.extractor.common.Extractor so no real HTTP class is touched."""
    import gallery_dl.extractor.common as common

    calls: list[tuple[str, Any]] = []

    class FakeSession:
        def __init__(self) -> None:
            self.hooks: dict[str, list[Any]] = {"response": []}

    class Fake:
        _interval_request: Any = None
        outcome: Any = FakeResponse(200)
        session: Any = None

        def request(self, url: str, *args: Any, **kwargs: Any) -> Any:
            calls.append((url, kwargs))
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            return self.outcome

        def _init_session(self) -> None:
            self.session = FakeSession()

    monkeypatch.setattr(common, "Extractor", Fake)
    Fake.calls = calls  # type: ignore[attr-defined]
    yield Fake
    pacing.uninstall()


def test_install_injects_the_delay_callable(fake_extractor: Any) -> None:
    p = _pacer()
    pacing.install(p)
    extr = fake_extractor()
    extr.request("https://x/")
    # gallery-dl reads the delay off the instance, so the patch must set it there.
    assert extr._interval_request() == p.delay
    assert extr._interval_request.__self__ is p
    assert p.requests == 1


def test_responses_are_judged_by_the_session_hook_not_the_return_value(
    fake_extractor: Any,
) -> None:
    """Judging the return value would score `429, 429, 200` as one clean response.

    Only a ``requests`` response hook fires per adapter send — so once per retry, once per
    redirect hop, and once per image download.
    """
    p = _pacer()
    pacing.install(p)
    extr = fake_extractor()
    extr._init_session()
    hook = extr.session.hooks["response"][0]

    extr.outcome = FakeResponse(429)
    extr.request("https://x/")
    assert p.delay == 1.0  # the wrapper itself judges nothing

    hook(FakeResponse(429), stream=False)
    assert p.delay == 30.0


def test_the_session_hook_never_raises_and_never_replaces_the_response(
    fake_extractor: Any,
) -> None:
    """A raising hook escapes Session.send; a non-None return replaces the response."""
    p = _pacer()
    pacing.install(p)
    extr = fake_extractor()
    extr._init_session()
    hook = extr.session.hooks["response"][0]

    class Hostile:
        @property
        def status_code(self) -> int:
            raise RuntimeError("boom")

    assert hook(Hostile(), stream=False) is None
    assert hook(FakeResponse(200), stream=False) is None


def test_install_penalizes_on_a_raised_request(fake_extractor: Any) -> None:
    p = _pacer()
    pacing.install(p)
    extr = fake_extractor()
    extr.outcome = RuntimeError("connection reset")
    with pytest.raises(RuntimeError):
        extr.request("https://x/")
    assert p.delay == 3.0
    assert p.requests == 1  # a failed request still counts toward the ramp


def test_install_is_idempotent_and_uninstall_restores_everything(fake_extractor: Any) -> None:
    original = fake_extractor.request
    original_init = fake_extractor._init_session
    root_handlers = len(logging.getLogger().handlers)
    p = _pacer()

    pacing.install(p)
    assert fake_extractor.request is not original
    assert fake_extractor.request.__wrapped__ is original
    assert len(logging.getLogger().handlers) == root_handlers + 1

    # Installing twice must not stack wrappers around the already-patched function.
    once = fake_extractor.request
    pacing.install(_pacer())
    assert fake_extractor.request is once
    assert len(logging.getLogger().handlers) == root_handlers + 1

    pacing.uninstall()
    assert fake_extractor.request is original
    assert fake_extractor._init_session is original_init
    assert len(logging.getLogger().handlers) == root_handlers
    pacing.uninstall()  # idempotent
    assert fake_extractor.request is original


def test_uninstall_neutralises_bindings_left_on_live_extractors(fake_extractor: Any) -> None:
    """Nothing un-binds `_interval_request`, so a leaked binding must stop sleeping."""
    p = _pacer(min_delay=9.0)
    pacing.install(p)
    extr = fake_extractor()
    extr.request("https://x/")
    assert extr._interval_request() == 9.0
    pacing.uninstall()
    assert extr._interval_request() == 0.0


def test_patch_forwards_arguments_untouched(fake_extractor: Any) -> None:
    pacing.install(_pacer())
    extr = fake_extractor()
    extr.request("https://x/", method="POST", interval=False, timeout=5)
    url, kwargs = fake_extractor.calls[-1]
    assert url == "https://x/"
    assert kwargs == {"method": "POST", "interval": False, "timeout": 5}


def test_unpaced_calls_still_count_toward_the_ramp(fake_extractor: Any) -> None:
    """``request_location``'s HEAD probes pass ``interval=False`` and never read the delay.

    gallery-dl skips its pacing block for those, so counting inside ``next_delay`` would undercount
    what the platform actually saw and let the ramp lag behind a long run.
    """
    p = _pacer()
    pacing.install(p)
    extr = fake_extractor()
    for _ in range(3):
        extr.request("https://x/", interval=False)
    assert p.requests == 3
