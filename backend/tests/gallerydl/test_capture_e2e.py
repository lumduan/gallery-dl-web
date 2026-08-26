"""The one claim behind body capture, executed rather than argued.

``telemetry.capture_body`` reads ``response.content`` from inside a ``requests`` response hook, on
the grounds that ``Session.send`` dispatches hooks at ``sessions.py:791`` and buffers the body
itself at ``sessions.py:827`` — so the read is free and changes nothing. That was established by
reading the ``requests`` source. **It had never been run.**

Every other test in this suite hands the pacer a hand-built stand-in, which cannot prove anything
about when ``requests`` actually populates a body. If the claim were wrong, the failure mode is
silent and expensive: a real Instagram run gets blocked, and every captured entry comes back with
``body: ""`` — evidence lost, and unrecoverable without provoking a second block.

So this drives a **real** ``requests.Session`` against a **loopback** server. It reaches no third
party: the autouse ``_no_real_http`` guard is opted out of by marker, not removed, and the server
binds ``127.0.0.1`` on an ephemeral port.

The negative control is the point of the file — delete the ``response.content`` read from
``telemetry.capture_body`` and this must go red.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import requests

from gallery_dl_web.gallerydl import telemetry

pytestmark = pytest.mark.localhost_http

# The envelope Instagram is hypothesised to return: HTTP 200, JSON, failure in the body. Whether
# this is the real wording is exactly what the capture run will settle — the shape is what matters
# here.
THROTTLE_BODY = json.dumps(
    {"status": "fail", "message": "Please wait a few minutes before you try again."}
).encode()

# An invented session cookie. If this string survives into a record, redaction is broken.
FAKE_SESSION = "5123%3AAbCdEf%3A17%3AAYc9xQfake"

# A stand-in for a media download: the bytes the sensor must never pull into memory.
MEDIA_BODY = b"\xff\xd8\xff\xe0" + b"\x00" * 4096


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        if self.path.startswith("/throttle"):
            body, ctype = THROTTLE_BODY, "application/json"
        elif self.path.startswith("/secret"):
            body = json.dumps({"sessionid": FAKE_SESSION, "status": "fail"}).encode()
            ctype = "application/json"
        else:
            body, ctype = MEDIA_BODY, "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # A Set-Cookie the recorder must not transcribe: only Content-Type/Length are allowlisted.
        self.send_header("Set-Cookie", f"sessionid={FAKE_SESSION}; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        """Silence the default stderr access log — pytest captures it as noise."""


@pytest.fixture
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, name="capture-e2e", daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _capture_through_a_real_hook(url: str, *, stream: bool) -> tuple[str, str]:
    """Run one real request and return what the sensor saw from inside the hook."""
    seen: list[tuple[str, str]] = []
    session = requests.Session()

    def _hook(response: Any, **kwargs: Any) -> None:
        seen.append(telemetry.capture_body(response, streamed=bool(kwargs.get("stream"))))

    session.hooks["response"].append(_hook)
    response = session.get(url, stream=stream)
    response.close()
    session.close()
    assert len(seen) == 1, "the hook must fire exactly once per adapter send"
    return seen[0]


def test_a_non_streamed_body_is_readable_from_inside_the_hook(server: str) -> None:
    """THE claim. `requests` has not buffered the body yet; reading it here must still work."""
    ctype, body = _capture_through_a_real_hook(f"{server}/throttle", stream=False)
    assert ctype.startswith("application/json")
    assert "Please wait a few minutes before you try again." in body, (
        "if this is empty the sensor is blind in production — the capture run would be wasted"
    )
    assert json.loads(body)["status"] == "fail"


def test_reading_early_does_not_break_the_response_for_the_caller(server: str) -> None:
    """The read must be transparent: gallery-dl calls .json() on the same object afterwards."""
    session = requests.Session()

    def _hook(response: Any, **kwargs: Any) -> None:
        # Must return None. `dispatch_hook` REPLACES the response with any non-None return, which
        # is why pacing.py's real hook is a `def` returning nothing rather than a lambda.
        telemetry.capture_body(response, streamed=bool(kwargs.get("stream")))

    session.hooks["response"].append(_hook)
    response = session.get(f"{server}/throttle")
    # This is what `Extractor.request_json` does next; it must still see the full body.
    assert response.json()["status"] == "fail"
    assert response.content == THROTTLE_BODY
    session.close()


def test_a_streamed_body_is_not_captured_and_not_consumed(server: str) -> None:
    """A media download must survive the sensor untouched, byte for byte."""
    seen: list[tuple[str, str]] = []
    session = requests.Session()
    session.hooks["response"].append(
        lambda r, **kw: seen.append(telemetry.capture_body(r, streamed=bool(kw.get("stream"))))
    )
    response = session.get(f"{server}/media", stream=True)

    assert seen[0][1] == "", "a streamed body must never be captured"
    # The download still works: the sensor must not have drained the socket.
    assert response.raw.read() == MEDIA_BODY or response.content == MEDIA_BODY
    session.close()


def test_credentials_do_not_survive_a_real_round_trip(server: str) -> None:
    """End-to-end redaction, against a live Set-Cookie header and a live body."""
    log = telemetry.RequestLog()
    session = requests.Session()

    def _hook(response: Any, **kwargs: Any) -> None:
        log.record(
            response,
            streamed=bool(kwargs.get("stream")),
            delay=4.0,
            floor=4.0,
            ceiling=30.0,
            classified="clean",
        )

    session.hooks["response"].append(_hook)
    session.get(f"{server}/secret?sessionid={FAKE_SESSION}")
    session.close()

    serialized = json.dumps(log.snapshot())
    assert FAKE_SESSION not in serialized, "a session cookie reached the telemetry record"
    assert "AYc9xQfake" not in serialized, "the cookie survived in some decoded form"
    assert "fail" in serialized, "redaction must not eat the diagnostic signal"
