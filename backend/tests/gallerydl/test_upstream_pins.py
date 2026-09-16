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

from gallery_dl import exception, job, util
from gallery_dl.extractor.common import Extractor
from gallery_dl.extractor.facebook import FacebookExtractor, FacebookPhotosExtractor


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


def test_facebook_still_returns_an_unkeyed_dict_for_a_profile_it_cannot_read() -> None:
    """The bug ``gallerydl/upstream_patches.py`` exists to work around.

    ``_extract_profile_page`` injects ``set_id`` only on its success branch, so both failure exits
    return a bare ``{}`` and ``FacebookPhotosExtractor.items`` raises ``KeyError: 'set_id'`` on it.
    Present in 1.32.9 and still in 1.32.12, so there is nothing to upgrade to. **When this test
    fails, upstream has fixed it — re-verify and delete the patch**, because a wrapper whose
    premise is gone is worse than no wrapper.
    """
    src = inspect.getsource(FacebookExtractor._extract_profile_page)
    assert "if set_id or user:" in src, "success branch not found — this pin is checking nothing"
    assert 'user["set_id"] = set_id' in src, "set_id injection moved — this pin checks nothing"
    assert "return {}" in src, (
        "gallery-dl no longer returns a bare {} for an unreadable profile page — re-verify and "
        "probably delete gallerydl/upstream_patches.py"
    )

    consumer = inspect.getsource(FacebookPhotosExtractor.items)
    assert "self.cache(" in consumer, "the caller moved — this pin is checking nothing"
    assert '["set_id"]' in consumer, (
        "FacebookPhotosExtractor no longer subscripts ['set_id'] — the crash site moved"
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
