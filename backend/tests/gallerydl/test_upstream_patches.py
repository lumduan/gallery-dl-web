"""The wrapper that turns gallery-dl's unreadable-Facebook-profile KeyError into a real failure.

These drive the patched method directly rather than building a live extractor: the whole surface
is "what does the wrapper do with what the original returned", and a stub keeps the test honest
about that instead of about gallery-dl's page parsing. Every test restores the class attribute in
a `finally`, so it never depends on fixture teardown ordering.
"""

from __future__ import annotations

from typing import Any

import pytest
from gallery_dl import exception
from gallery_dl.extractor.facebook import FacebookExtractor, FacebookPhotosExtractor

from gallery_dl_web.gallerydl import upstream_patches
from gallery_dl_web.gallerydl.errors import (
    EMPTY_PROFILE_MARKER,
    detect_empty_profile,
    detect_login_wall,
)

_URL = "https://www.facebook.com/placeholder/photos_by"


def _extractor(cls: type = FacebookExtractor) -> Any:
    """A real instance without ``__init__`` — enough for ``self.exc`` and ``self.cache``."""
    return cls.__new__(cls)


def _with_stub(result: Any, body: Any) -> Any:
    """Run ``body(...)`` with ``_extract_profile_page`` stubbed to return ``result``, patched."""
    original = FacebookExtractor._extract_profile_page
    FacebookExtractor._extract_profile_page = lambda self, url: result
    try:
        stop = upstream_patches.install()
        try:
            return body()
        finally:
            stop()
    finally:
        FacebookExtractor._extract_profile_page = original


def test_an_unreadable_profile_raises_instead_of_returning_an_empty_dict() -> None:
    """The bug: `_extract_profile_page` returns `{}`, and the caller subscripts `["set_id"]`."""

    def body() -> Any:
        with pytest.raises(exception.AuthRequired) as excinfo:
            FacebookExtractor._extract_profile_page(_extractor(), _URL)
        return excinfo.value

    exc = _with_stub({}, body)
    assert EMPTY_PROFILE_MARKER in str(exc)
    # The wording `errors.py` keys the reported reason off. Not a coincidence worth trusting
    # silently: gallery-dl composes it in `exception.AuthRequired.__init__`.
    assert "authenticated cookies needed" in str(exc)
    assert exc.code == 16, "the exit-status bit `map_exit_status` maps to `login-required`"


def test_an_empty_set_id_on_a_readable_profile_is_not_a_failure() -> None:
    """The one that would break healthy profiles if the wrapper tested the value, not the dict.

    `info` and `avatar` ask for `/{profile}`, which carries no photo-set id at all, so `set_id` is
    legitimately `""` there. Only a wholly empty dict means "could not read the page".
    """
    user = {"set_id": "", "id": "1", "username": "placeholder"}
    returned = _with_stub(user, lambda: FacebookExtractor._extract_profile_page(_extractor(), _URL))
    assert returned is user


def test_the_photos_extractor_no_longer_dies_with_a_keyerror() -> None:
    """End of the chain: `items()` is where the operator-visible crash actually happened."""

    def body() -> Any:
        extr = _extractor(FacebookPhotosExtractor)
        extr.groups = ("placeholder",)
        with pytest.raises(exception.AuthRequired):
            # `items()` is a generator-free method here: it raises before returning an iterator.
            FacebookPhotosExtractor.items(extr)
        return None

    _with_stub({}, body)


def test_a_failed_read_is_not_memoized_for_the_avatar_to_inherit() -> None:
    """`Extractor.cache` keys on the profile name alone, ignoring the `set_id` argument.

    So a returned `{}` is handed straight back to `FacebookAvatarExtractor`, which is why a failed
    run logs the avatar finding "No results" without making a request. Raising stores nothing.
    """
    from gallery_dl.extractor.common import CACHE_MEMORY

    key = "gallery_dl.extractor.facebook._extract_profile-placeholder"
    CACHE_MEMORY.pop(key, None)

    def body() -> None:
        extr = _extractor(FacebookPhotosExtractor)
        extr.groups = ("placeholder",)
        with pytest.raises(exception.AuthRequired):
            FacebookPhotosExtractor.items(extr)

    try:
        _with_stub({}, body)
        assert key not in CACHE_MEMORY, "a failed profile read was memoized for the whole process"
    finally:
        CACHE_MEMORY.pop(key, None)


def test_uninstall_restores_the_original_and_is_idempotent() -> None:
    original = FacebookExtractor._extract_profile_page
    stop = upstream_patches.install()
    assert FacebookExtractor._extract_profile_page is not original
    stop()
    assert FacebookExtractor._extract_profile_page is original
    stop()  # second call must be a no-op, not a second restore
    upstream_patches.uninstall()
    assert FacebookExtractor._extract_profile_page is original


def test_install_is_idempotent() -> None:
    original = FacebookExtractor._extract_profile_page
    first = upstream_patches.install()
    try:
        patched = FacebookExtractor._extract_profile_page
        assert upstream_patches.install() is first
        assert FacebookExtractor._extract_profile_page is patched, "double-wrapped"
    finally:
        first()
    assert FacebookExtractor._extract_profile_page is original


def test_the_classifier_matches_exactly_what_the_patch_raises() -> None:
    """The anti-drift test, and the most valuable one here.

    ``upstream_patches`` raises it and ``errors.py`` has to catch it; they live in different files
    and share only ``EMPTY_PROFILE_MARKER``. If either side is reworded without the other, the
    operator silently goes back to reading a raw stderr tail.
    """

    def body() -> Any:
        with pytest.raises(exception.AuthRequired) as excinfo:
            FacebookExtractor._extract_profile_page(_extractor(), _URL)
        return excinfo.value

    rendered = f"error:facebook:AuthRequired: {_with_stub({}, body)}"
    assert detect_empty_profile([rendered]) is True
    # And the independent fallback layer, which is what makes the reason survive a reworded rule.
    assert detect_login_wall([rendered]) is True


def test_install_is_a_noop_when_the_upstream_seam_moved() -> None:
    """A gallery-dl bump that changes the signature must not get a broken wrapper.

    Wrapping it anyway would turn a classified failure into a TypeError on every Facebook job —
    strictly worse than the bug. Skipping leaves the KeyError, which ``errors.py`` still catches.
    """
    original = FacebookExtractor._extract_profile_page
    moved = lambda self, url, extra=None: {}  # noqa: E731 - a stand-in for a moved upstream seam
    FacebookExtractor._extract_profile_page = moved
    try:
        stop = upstream_patches.install()
        try:
            assert FacebookExtractor._extract_profile_page is moved, "wrapped a seam that moved"
        finally:
            stop()
    finally:
        FacebookExtractor._extract_profile_page = original
