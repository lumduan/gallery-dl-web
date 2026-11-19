"""The session-cookie watch: a logged-out session must be told apart from a rate-limit block.

The response fixtures are shaped like what Instagram sent on 2026-11-19 to a session it had logged
out: the profile page answered 302 to the bare home page with a ``Set-Cookie`` that expired
``sessionid`` (``expires`` in 1970 AND a negative ``Max-Age``), then the home page answered 200.
The cookie value is a placeholder: the real one is irrelevant to the decision, and this module
must never read it out anyway.

The end-to-end tests drive gallery-dl's real ``_init_session`` and a real ``requests.Session``,
which follows the redirect itself and dispatches the hook per hop, exactly as in production. Only
the transport is replaced, by an adapter that serves canned urllib3 responses; nothing reaches the
network.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3._collections import HTTPHeaderDict

from gallery_dl_web.gallerydl import session_watch
from gallery_dl_web.gallerydl.errors import SESSION_LOGGED_OUT_MARKER

# A placeholder in the shape of a deletion; its value must never show up in anything we produce.
PLACEHOLDER = "PLACEHOLDER-VALUE-7"
LOGOUT = (
    f"sessionid={PLACEHOLDER}; Domain=.instagram.com; expires=Thu, 01 Jan 1970 00:00:00 GMT; "
    "HttpOnly; Max-Age=-1700000000; Path=/; Secure"
)
CSRF = "csrftoken=abc123; Domain=.instagram.com; expires=Fri, 19 Nov 2027 07:35:28 GMT; Max-Age=31449600; Path=/; Secure"  # noqa: E501
ROTATION = (
    "sessionid=NEWVALUE; Domain=.instagram.com; expires=Fri, 19 Nov 2027 07:35:28 GMT; "
    "HttpOnly; Max-Age=31536000; Path=/; Secure"
)


def _raw(status: int, set_cookies: tuple[str, ...] = (), location: str | None = None) -> Any:
    headers = HTTPHeaderDict()
    for line in set_cookies:
        headers.add("Set-Cookie", line)
    if location:
        headers.add("Location", location)
    headers.add("Content-Type", "text/html; charset=utf-8")
    return urllib3.HTTPResponse(
        body=io.BytesIO(b""), headers=headers, status=status, preload_content=False
    )


def _response(url: str, *set_cookies: str, status: int = 302) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r.url = url
    r.raw = _raw(status, set_cookies)
    return r


# --- the parser ----------------------------------------------------------------------------------


class TestDeletedCookies:
    def test_the_observed_instagram_logout_is_a_deletion(self) -> None:
        r = _response("https://www.instagram.com/someprofile/", CSRF, LOGOUT)
        assert session_watch.deleted_cookies(r) == ("sessionid",)
        assert session_watch.session_logged_out(r) == ("instagram.com", "sessionid")

    def test_a_rotated_session_is_not_a_logout(self) -> None:
        """A fresh sessionid with a future lifetime is a refresh. Treating it as a logout would
        send a healthy run to Settings."""
        r = _response("https://www.instagram.com/someprofile/", ROTATION)
        assert session_watch.deleted_cookies(r) == ()
        assert session_watch.session_logged_out(r) is None

    @pytest.mark.parametrize(
        ("attributes", "deleted"),
        [
            ("Max-Age=0", True),
            ("Max-Age=-1", True),
            ("expires=Thu, 01 Jan 1970 00:00:00 GMT", True),
            ("expires=Thu, 01-Jan-1970 00:00:00 GMT", True),  # Netscape's dashed form
            ("expires=Fri, 19 Nov 2027 07:35:28 GMT", False),
            # RFC 6265 5.3: Max-Age outranks Expires, in both directions.
            ("expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=3600", False),
            ("expires=Fri, 19 Nov 2027 07:35:28 GMT; Max-Age=0", True),
            ("expires=not a date", False),
            ("Max-Age=soon", False),
            ("Path=/", False),  # a session cookie with no lifetime is not a deletion
        ],
    )
    def test_lifetime_attributes(self, attributes: str, deleted: bool) -> None:
        r = _response("https://www.instagram.com/", f"sessionid=x; {attributes}")
        assert (session_watch.deleted_cookies(r) == ("sessionid",)) is deleted

    def test_it_never_returns_a_value(self) -> None:
        r = _response("https://www.instagram.com/", LOGOUT, CSRF)
        assert PLACEHOLDER not in repr(session_watch.deleted_cookies(r))

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.facebook.com/",  # not observed there, so not guessed
            "https://evilinstagram.com/",  # a suffix match must respect the dot
            "not a url",
        ],
    )
    def test_only_instagram_hosts_count(self, url: str) -> None:
        assert session_watch.session_logged_out(_response(url, LOGOUT)) is None

    def test_a_subdomain_counts(self) -> None:
        r = _response("https://i.instagram.com/api/v1/x/", LOGOUT)
        assert session_watch.session_logged_out(r) == ("instagram.com", "sessionid")

    @pytest.mark.parametrize("response", [object(), None, requests.Response()])
    def test_never_raises_on_something_that_is_not_a_full_response(self, response: Any) -> None:
        assert session_watch.deleted_cookies(response) == ()
        assert session_watch.session_logged_out(response) is None


# --- the hook, end to end through requests --------------------------------------------------------


class _CannedAdapter(HTTPAdapter):
    """Serve canned responses by path. ``_no_real_http`` patches HTTPAdapter.send itself; this
    override never calls it, so the guard stays in force for anything else."""

    def __init__(self, routes: dict[str, Any]) -> None:
        super().__init__()
        self.routes = routes

    def send(self, request: Any, *_args: Any, **_kwargs: Any) -> requests.Response:  # type: ignore[override]
        path = request.path_url.split("?")[0]
        return self.build_response(request, self.routes[path]())


@pytest.fixture
def installed() -> Iterator[None]:
    session_watch.install()
    try:
        yield
    finally:
        session_watch.uninstall()


def _extractor_session(routes: dict[str, Any]) -> requests.Session:
    from gallery_dl import extractor

    ex = extractor.find("https://www.instagram.com/someprofile/")
    ex._init_session()  # the real gallery-dl method, wrapped by install()
    ex.session.mount("https://", _CannedAdapter(routes))
    return ex.session  # type: ignore[no-any-return]


LOGGED_OUT_ROUTES = {
    "/someprofile/": lambda: _raw(302, (CSRF, LOGOUT), location="https://www.instagram.com/"),
    "/": lambda: _raw(200),
}


@pytest.mark.usefixtures("installed")
def test_the_observed_bounce_logs_the_marker_once(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="gallery_dl_web.gallerydl.session_watch")
    session = _extractor_session(LOGGED_OUT_ROUTES)

    r = session.get("https://www.instagram.com/someprofile/")
    assert r.status_code == 200 and r.history[0].status_code == 302, "the bounce was not followed"
    # A second child extractor gets logged out again with the same stored cookie.
    _extractor_session(LOGGED_OUT_ROUTES).get("https://www.instagram.com/someprofile/")

    hits = [rec for rec in caplog.records if SESSION_LOGGED_OUT_MARKER in rec.getMessage()]
    assert len(hits) == 1, "once per process: every child extractor repeats the same logout"
    assert hits[0].levelno == logging.WARNING  # survives the worker's basicConfig(WARNING)
    assert "'sessionid'" in hits[0].getMessage()
    assert PLACEHOLDER not in caplog.text, "a cookie value reached the log"


@pytest.mark.usefixtures("installed")
def test_a_healthy_run_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="gallery_dl_web.gallerydl.session_watch")
    session = _extractor_session(
        {"/someprofile/": lambda: _raw(200, (CSRF, ROTATION)), "/": lambda: _raw(200)}
    )
    session.get("https://www.instagram.com/someprofile/")
    assert not caplog.records


@pytest.mark.usefixtures("installed")
def test_a_broken_response_never_reaches_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raising out of a requests hook escapes Session.send and would fail the download."""

    def _boom(_response: Any) -> Any:
        raise RuntimeError("parser bug")

    monkeypatch.setattr(session_watch, "session_logged_out", _boom)
    r = _extractor_session(LOGGED_OUT_ROUTES).get("https://www.instagram.com/someprofile/")
    assert r.status_code == 200


def test_install_is_idempotent_and_uninstall_restores() -> None:
    from gallery_dl.extractor.common import Extractor

    pristine = Extractor._init_session
    first = session_watch.install()
    try:
        assert session_watch.install() is first
        assert Extractor._init_session is not pristine
    finally:
        session_watch.uninstall()
    assert Extractor._init_session is pristine
    session_watch.uninstall()  # a second uninstall is a no-op
    assert Extractor._init_session is pristine
