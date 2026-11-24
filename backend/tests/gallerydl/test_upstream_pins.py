"""Assumptions this project makes about gallery-dl's internals, asserted against the installed copy.

Everything here is *upstream* behaviour we depend on but do not control. A gallery-dl bump that
changes one of these does not raise — it quietly changes what our config means — so each is pinned
where a version bump will trip it.

Every pin asserts that its markers were **found** before it compares them. A source-scanning check
whose pattern stops matching would otherwise report success for the same reason a `files:` pattern
matching zero files reports "Skipped": nothing was checked, and nothing said so.
"""

from __future__ import annotations

import inspect

import pytest
from gallery_dl import exception, job, util
from gallery_dl.downloader.http import HttpDownloader
from gallery_dl.extractor.common import Extractor
from gallery_dl.extractor.facebook import FacebookExtractor, FacebookPhotosExtractor
from gallery_dl.extractor.instagram import InstagramExtractor

from gallery_dl_web.config import Settings
from gallery_dl_web.gallerydl.config_builder import _PER_FILE_JITTER, _PLATFORM_DEFAULTS


def test_a_per_file_sleep_is_never_charged_on_a_skip() -> None:
    """The property that keeps re-running a downloaded profile fast.

    ``config_builder`` sets gallery-dl's ``sleep`` to put a floor between image downloads. That is
    only tolerable because ``DownloadJob.handle_url`` returns on both skip paths — the archive
    check and the on-disk check — *before* it reaches the sleep. If a future gallery-dl moved the
    sleep above them, every archived file in a re-run would pay the full per-image delay and a
    2000-file profile refresh would go from seconds to over an hour.
    """
    src = inspect.getsource(job.DownloadJob.handle_url)
    archive = src.find("archive.check(kwdict)")
    on_disk = src.find("pathfmt.exists()")
    sleep = src.find('self.sleep(), "download"')

    assert archive >= 0, "archive skip check not found — this pin is not checking anything"
    assert on_disk >= 0, "on-disk skip check not found — this pin is not checking anything"
    assert sleep >= 0, "per-download sleep call not found — this pin is not checking anything"
    assert archive < sleep, "the archive skip no longer returns before the per-download sleep"
    assert on_disk < sleep, "the on-disk skip no longer returns before the per-download sleep"


def test_zero_really_disables_the_per_file_sleep() -> None:
    """``0`` must mean "off", not "sleep zero seconds".

    ``DownloadJob.initialize`` stores ``build_duration_func(cfg("sleep", ...))`` and the call site
    is guarded by ``if self.sleep is not None``. A None here is what makes the disabled path
    byte-for-byte the behaviour from before the per-image delay existed, rather than a no-op call
    on every single file.
    """
    assert util.build_duration_func(0.0) is None
    assert util.build_duration_func(0) is None


def test_a_two_element_range_is_sampled_per_call() -> None:
    """``config_builder`` emits a band rather than a constant so the gaps are not periodic. That
    only works if gallery-dl re-samples it per download instead of fixing it for the run."""
    f = util.build_duration_func([1.7, 2.3])
    assert f is not None
    samples = {round(f(), 3) for _ in range(200)}
    assert len(samples) > 1, "the band is not being re-sampled — every gap would be identical"
    assert all(1.7 <= s <= 2.3 for s in samples), f"sample outside the requested band: {samples}"


def test_the_per_file_sleep_is_additive_not_a_spacing_floor() -> None:
    """The one place `sleep` differs from `sleep-request`, and the reason the band is centred on
    the operator's number rather than starting at it.

    ``Extractor.request`` subtracts elapsed time from ``_interval_request`` (a floor on spacing);
    ``Extractor.sleep`` does not (a plain addend). Were that to change, the observed per-image gap
    would drop by however long the download took.
    """
    assert "request_timestamp" in inspect.getsource(Extractor.request)
    assert "request_timestamp" not in inspect.getsource(Extractor.sleep)


def test_an_image_429_is_retried_after_a_sleep_429_wait() -> None:
    """What turns a CDN 429 on an image into silent sleep instead of one failed file.

    Up to 1.32.19 an image that answered 429 failed at once, because 429 was not in the default
    ``retry-codes``. 1.32.20 adds it whenever ``sleep-429`` resolves to a duration, which it always
    does for us, and before each retry sleeps ``max(tries, interval_429(tries))``. Nothing is
    emitted during that sleep (no ``prepare``, no ``file``), so it spends the manager's progress
    deadline. The test below bounds it; this one pins the formula that test models.
    """
    init = inspect.getsource(HttpDownloader.__init__)
    impl = inspect.getsource(HttpDownloader._download_impl)
    options = inspect.getsource(Extractor._init_options)

    assert "[*extractor._retry_codes, 429]" in init, (
        "the downloader no longer adds 429 to its retry codes — re-check what an image 429 does"
    )
    assert 'self.config("retries", extractor._retries)' in init, "retry count source moved"
    assert 'self.config("retries", 4)' in options, "the default retry count moved or changed"
    for marker in (
        "if tries > self.retries:",
        "seconds = self.interval_retry(tries)",
        "s = self.interval_429(tries)",
        "if seconds < s:",
        "time.sleep(seconds)",
    ):
        assert marker in impl, f"{marker!r} not found — the retry sleep this pin models changed"


@pytest.mark.parametrize(
    ("platform", "extractor"),
    [("instagram", InstagramExtractor), ("facebook", FacebookExtractor)],
)
def test_the_worst_image_429_retry_sleep_stays_inside_the_progress_deadline(
    platform: str, extractor: type[Extractor]
) -> None:
    """One image's worst case must fit between two progress events, at the shipped defaults.

    After the first file, the progress deadline is at least ``stall_floor_seconds``, and each
    image's ``prepare`` resets it. The longest an image can then stay silent is the top of its
    per-image delay band plus every retry sleep above. Today that is about 242 s on Instagram
    (2.3 + 4 x 60, gallery-dl's default ``sleep-429``) and 165 s on Facebook (15 + 30 + 60 + 60,
    our shaped value), against 300 s. Raising ``retries`` or ``sleep-429``, or a gallery-dl that
    sleeps longer, would let one rate-limited image get a healthy job killed as stalled.

    Known edge, not covered here: Settings accepts a per-image delay up to 60 s. With an
    Instagram value above about 50 s the sum passes 300 s.
    """
    defaults = _PLATFORM_DEFAULTS[platform]
    interval_429 = util.build_duration_func_ex(
        defaults.get("sleep-429", extractor.request_interval_429)
    )
    assert interval_429 is not None, "sleep-429 resolved to nothing — this pin checks nothing"
    retries = defaults.get("retries", 4)
    retry_sleep = sum(max(t, interval_429(t)) for t in range(1, retries + 1))

    per_file = Settings.model_fields[f"{platform}_sleep_file"].default
    assert isinstance(per_file, float)
    floor = Settings.model_fields["stall_floor_seconds"].default
    assert isinstance(floor, float)

    worst = per_file * (1.0 + _PER_FILE_JITTER) + retry_sleep
    assert worst < floor, (
        f"{platform}: one image can sleep {worst:.0f} s on 429 retries, "
        f"past the {floor:.0f} s progress deadline"
    )


def test_facebook_still_returns_an_unkeyed_dict_for_a_profile_it_cannot_read() -> None:
    """The premise ``gallerydl/upstream_patches.py`` rests on.

    ``_extract_profile_page`` injects ``set_id`` only on its success branch, so both failure exits
    return a bare ``{}``. Present in 1.32.9 and still in 1.32.12, 1.32.15 and 1.32.19. **When this
    half fails, re-verify the patch** — a wrapper whose premise is gone is worse than no wrapper.
    """
    src = inspect.getsource(FacebookExtractor._extract_profile_page)
    assert "if set_id or user:" in src, "success branch not found — this pin is checking nothing"
    assert 'user["set_id"] = set_id' in src, "set_id injection moved — this pin checks nothing"
    assert "return {}" in src, (
        "gallery-dl no longer returns a bare {} for an unreadable profile page — re-verify and "
        "probably delete gallerydl/upstream_patches.py"
    )


def test_unpatched_facebook_photos_turn_an_unreadable_profile_into_a_silent_empty_result() -> None:
    """What the patch prevents, as upstream now ships it.

    Up to 1.32.18 ``FacebookPhotosExtractor.items`` subscripted ``["set_id"]`` on the ``{}`` above
    and crashed with ``KeyError``. 1.32.19 "fixed" that (cb#247) into exactly the outcome the patch
    was written to avoid: a WARNING, then ``return iter(())`` — status 0, zero files, a run that
    reports success. **That fix is not a reason to delete the patch.** If this test fails, read
    what the consumer does now before deciding the patch is redundant: it is redundant only if an
    unreadable profile makes the run FAIL.
    """
    consumer = inspect.getsource(FacebookPhotosExtractor.items)
    assert "self.cache(self._extract_profile_page" in consumer, (
        "the caller no longer goes through _extract_profile_page — the patch may not be reached"
    )
    assert '.get("set_id")' in consumer, "set_id lookup moved — this pin is checking nothing"
    assert "if not set_id:" in consumer, "the empty-result branch moved — this pin checks nothing"
    assert "return iter(())" in consumer, (
        "an unreadable profile no longer yields an empty result — re-verify whether the run now "
        "fails on its own, and only then consider deleting gallerydl/upstream_patches.py"
    )


def test_the_facebook_profile_helper_still_has_the_shape_the_patch_wraps() -> None:
    """``upstream_patches`` skips itself with a warning if this changes; the warning is easy to
    miss in a worker's stderr, so the bump should fail here instead."""
    params = tuple(inspect.signature(FacebookExtractor._extract_profile_page).parameters)
    assert params == ("self", "url"), f"upstream_patches.install() will no-op: {params}"


def test_auth_failures_still_carry_exit_bit_16() -> None:
    """What ``events.map_exit_status`` maps to ``login-required``."""
    assert exception.AuthRequired.code == 16
    assert exception.AuthorizationError.code == 16
    assert exception.AuthenticationError.code == 16
    # ...and the bit that must keep outranking it, because Extractor.status accumulates it.
    assert exception.HttpError.code == 4
    assert exception.NotFoundError.code == 4


def test_an_extractor_error_in_one_dispatched_child_does_not_abort_its_siblings() -> None:
    """Why raising from the patch is safe.

    A Facebook profile URL dispatches up to four child extractors. ``AuthRequired`` is a plain
    ``GalleryDLException``, so the child absorbs it into its own status and the parent ORs that in
    — the avatar still runs after the photos child fails. Were it ever re-classified as one of the
    control exceptions, it would escape ``DownloadJob.run`` entirely and the worker would report
    ``worker-crash`` instead, which is strictly worse than the bug being patched.
    """
    run = inspect.getsource(job.Job.run)
    assert "except exception.GalleryDLException as exc:" in run, (
        "the handler moved — this pin is checking nothing"
    )
    assert "self.status |= exc.code" in run, "status accumulation moved — this pin checks nothing"
    assert "self.status |= extractor.status" in run, (
        "Job.run no longer folds in extractor.status — re-verify the map_exit_status bit ordering"
    )

    queue = inspect.getsource(job.DownloadJob.handle_queue)
    assert "job.run()" in queue, "child dispatch moved — this pin is checking nothing"
    assert not issubclass(exception.AuthRequired, exception.ControlException), (
        "AuthRequired became a control exception — it would now escape the child job"
    )
