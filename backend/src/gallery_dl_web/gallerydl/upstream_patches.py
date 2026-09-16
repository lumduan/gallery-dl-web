"""Patches for defects in the installed gallery-dl, applied inside the worker process.

Separate from ``pacing.install`` on purpose: pacing is only installed in ``adaptive`` mode, and
these must apply to every run. Same shape though — patch the *class*, because profile extraction
spawns child jobs with their own extractor objects — and the same posture: a failure to install
degrades to unpatched gallery-dl rather than costing the job.

That degradation is only safe because every patch here has a second, independent sensor on the
manager's side. This one's is ``errors.py:detect_empty_profile``, so a run where ``install()``
silently did nothing still reports the right reason.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any

from gallery_dl_web.gallerydl.errors import EMPTY_PROFILE_MARKER

logger = logging.getLogger(__name__)

_INSTALLED: Any = None


def _noop() -> None:
    return None


def install() -> Callable[[], None]:
    """Apply every patch. Idempotent, never raises; returns a callable that restores the originals.

    A patch whose upstream seam has moved is skipped with a warning rather than forced: wrapping a
    method whose signature changed would turn a classified failure into a ``TypeError``, which is
    strictly worse than the bug being patched.
    """
    global _INSTALLED
    if _INSTALLED is not None:
        return _INSTALLED  # type: ignore[no-any-return]

    restores: list[Callable[[], None]] = []
    try:
        restores.append(_patch_facebook_empty_profile())
    except Exception:
        logger.exception("upstream gallery-dl patches could not be installed; continuing unpatched")
        for restore in reversed(restores):
            restore()
        return _noop

    def _uninstall() -> None:
        global _INSTALLED
        if _INSTALLED is None:
            return
        for restore in reversed(restores):
            restore()
        _INSTALLED = None

    _INSTALLED = _uninstall
    return _uninstall


def uninstall() -> None:
    """Undo ``install``. Idempotent."""
    if _INSTALLED is not None:
        _INSTALLED()


def _patch_facebook_empty_profile() -> Callable[[], None]:
    """Make an unreadable Facebook profile a classified failure instead of a ``KeyError``.

    ``FacebookExtractor._extract_profile_page`` retries a page it cannot parse and then returns a
    bare ``{}`` — with no ``set_id`` key. ``FacebookPhotosExtractor.items`` immediately subscripts
    ``["set_id"]`` on it, so the operator gets ``KeyError: 'set_id'`` plus gallery-dl's "report this
    issue on codeberg" text. Verified identical in 1.32.9 (the installed pin) and 1.32.12 (the
    newest release), so there is nothing to upgrade to — ``tests/gallerydl/test_upstream_pins.py``
    is what tells us when that stops being true.

    **This deliberately does NOT do what upstream meant.** The line after the crash site is
    ``if not set_id: return iter(())``, so the intent was a silent empty result. Restoring that
    would give a total failure ``status 0`` / ``reason "ok"`` / zero files — a *silent success*,
    which for a profile downloader is worse than the crash. Do not "fix" this back.

    ``AuthRequired`` rather than a custom exception, for three reasons: it is upstream's own idiom
    for the sibling failure ten lines above (the "This content isn't available right now" page);
    ``job.py`` catches ``GalleryDLException`` into one clean log line and ``status |= 16`` instead
    of the unexpected-error path; and its rendered text contains "authenticated cookies needed",
    which ``errors.py`` already classifies as a login wall even with every other edit reverted.

    Raising also un-poisons the avatar. ``Extractor.cache`` keys on the profile name alone — the
    ``set_id=True/False`` argument is not part of the key and ``_exp=0`` memoizes for the life of
    the process — so today's ``{}`` from ``/photos_by`` is handed straight back to
    ``FacebookAvatarExtractor``, which is why a failed run logs the avatar finding "No results"
    with no request of its own. Nothing is memoized when the call raises, so the avatar fetches its
    own page and can still succeed on a profile whose ``/`` parses even though ``/photos_by`` does
    not. It costs one extra pair of requests on a profile that is genuinely walled; that is the
    price of the avatar working on one that is only partly walled.
    """
    from gallery_dl.extractor.facebook import FacebookExtractor

    original = FacebookExtractor._extract_profile_page
    if not _has_shape(original, ("self", "url")):
        logger.warning(
            "FacebookExtractor._extract_profile_page no longer takes (self, url); the "
            "empty-profile patch was NOT installed — see gallerydl/upstream_patches.py"
        )
        return _noop

    @functools.wraps(original)
    def _extract_profile_page(self: Any, url: str) -> Any:
        result = original(self, url)
        if not result:
            # The whole dict, never `result["set_id"]`: an empty set id is *normal* for the
            # `/{profile}` URL that the info and avatar extractors ask for, and testing the value
            # would fail those on healthy profiles. `{}` is the only "could not read the page"
            # signal, because the success branch returns as soon as *either* marker parses.
            raise self.exc.AuthRequired("authenticated cookies", "profile", EMPTY_PROFILE_MARKER)
        return result

    FacebookExtractor._extract_profile_page = _extract_profile_page

    def _restore() -> None:
        FacebookExtractor._extract_profile_page = original

    return _restore


def _has_shape(func: Any, params: tuple[str, ...]) -> bool:
    """True if ``func`` still takes exactly ``params`` — the seam a wrapper assumes."""
    try:
        return tuple(inspect.signature(func).parameters) == params
    except (TypeError, ValueError):
        return False
