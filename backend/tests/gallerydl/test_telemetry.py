"""Per-request evidence capture: what may be read, and what must never leave the process.

No network. Responses are hand-built stand-ins, as in ``test_pacing.py`` — but here ``content`` is
readable, because reading it is the thing under test.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gallery_dl_web.gallerydl import telemetry

# A real Instagram session cookie shape, invented. If any of these strings can be found in a
# serialized entry, the redaction is broken.
SESSION_ID = "51234567890%3AAbCdEfGhIjKlMn%3A17%3AAYc9xQfakevaluenotreal"
CSRF = "kZ9QfakeCsrfTokenValue00"


class Resp:
    """A response with a readable body — the streamed/buffered guards are passed explicitly."""

    def __init__(
        self,
        status: int = 200,
        url: str = "https://i.instagram.com/api/v1/feed/user/1/",
        body: bytes = b"{}",
        ctype: str = "application/json",
        length: str | None = None,
        consumed: bool = False,
    ) -> None:
        self.status_code = status
        self.url = url
        self.content = body
        self._content_consumed = consumed
        self.headers: dict[str, str] = {}
        if ctype:
            self.headers["content-type"] = ctype
        if length is not None:
            self.headers["content-length"] = length


def _entry(response: Any, **over: Any) -> dict[str, Any]:
    log = telemetry.RequestLog()
    kwargs: dict[str, Any] = {
        "streamed": False,
        "delay": 4.0,
        "floor": 4.0,
        "ceiling": 30.0,
        "classified": "clean",
    }
    kwargs.update(over)
    log.record(response, **kwargs)
    entries = log.snapshot()
    assert len(entries) == 1
    return entries[0]


# --- what may be read ----------------------------------------------------------------------------


def test_a_non_streamed_json_body_is_captured() -> None:
    """`requests` buffers this body 36 lines later anyway, so reading it early is free."""
    body = b'{"status": "fail", "message": "Please wait a few minutes before you try again."}'
    entry = _entry(Resp(body=body))
    assert "Please wait a few minutes before you try again." in entry["body"]
    assert entry["content_type"] == "application/json"


def test_a_streamed_body_is_never_read() -> None:
    """Every media download is streamed; reading one buffers the whole file into RAM."""

    class Exploding(Resp):
        @property  # type: ignore[misc]
        def content(self) -> bytes:
            raise AssertionError("a streamed body must never be read")

        @content.setter
        def content(self, value: bytes) -> None:
            pass

    entry = _entry(Exploding(ctype="application/json"), streamed=True)
    assert entry["body"] == ""
    assert entry["streamed"] is True


def test_an_already_buffered_streamed_body_is_safe_to_read() -> None:
    """`_content_consumed` means the bytes are in memory already — no download is re-buffered."""
    entry = _entry(Resp(body=b'{"ok": 1}', consumed=True), streamed=True)
    assert '{"ok": 1}' in entry["body"]


@pytest.mark.parametrize("ctype", ["image/jpeg", "video/mp4", "application/octet-stream", ""])
def test_non_text_content_types_are_skipped_before_a_byte_is_touched(ctype: str) -> None:
    entry = _entry(Resp(body=b"binary", ctype=ctype))
    assert entry["body"] == ""


def test_a_body_over_the_size_threshold_is_skipped() -> None:
    big = str(telemetry.MAX_CAPTURE_BYTES + 1)
    assert _entry(Resp(body=b'{"a": 1}', length=big))["body"] == ""
    assert _entry(Resp(body=b'{"a": 1}', length="10"))["body"] != ""


def test_the_prefix_is_truncated() -> None:
    entry = _entry(Resp(body=b'{"k":"' + b"a" * 5000 + b'"}'))
    assert len(entry["body"]) <= telemetry.BODY_PREFIX_BYTES


# --- what must never leave -----------------------------------------------------------------------


def test_no_cookie_or_token_value_can_reach_an_entry() -> None:
    """The guarantee the free-form body prefix cannot get from an allowlist.

    Asserted against the *serialized* entry, because that is what actually crosses the process
    boundary and lands in an SSE payload — an object-level check would miss a nested value.
    """
    body = json.dumps(
        {
            "sessionid": SESSION_ID,
            "csrftoken": CSRF,
            "authorization": "Bearer " + "z" * 40,
            "status": "fail",
            "message": "Please wait a few minutes before you try again.",
        }
    ).encode()
    entry = _entry(
        Resp(
            body=body,
            url=f"https://scontent.cdninstagram.com/v/t51.jpg?sessionid={SESSION_ID}&oe=deadbeef",
        )
    )
    serialized = json.dumps(entry)

    assert SESSION_ID not in serialized
    assert CSRF not in serialized
    assert "Bearer" not in serialized or "z" * 40 not in serialized
    # The diagnostic signal itself must survive redaction, or the capture is pointless.
    assert "Please wait a few minutes before you try again." in serialized
    assert "fail" in serialized


def test_the_url_keeps_host_and_path_and_drops_the_query_whole() -> None:
    """Instagram signs media URLs in the query; it is dropped, not masked."""
    entry = _entry(Resp(url="https://i.instagram.com/api/v1/feed/?max_id=abc&sig=" + "f" * 40))
    assert entry["url"] == "i.instagram.com/api/v1/feed/"
    assert "max_id" not in entry["url"]


def test_credentials_in_the_url_are_dropped() -> None:
    entry = _entry(Resp(url="https://user:hunter2@i.instagram.com/api/"))
    assert "hunter2" not in entry["url"]
    assert entry["url"] == "i.instagram.com/api/"


def test_request_headers_are_never_read() -> None:
    """Cookie and Authorization live on the request; the recorder must not reach for them."""

    class Trap(Resp):
        @property
        def request(self) -> Any:
            raise AssertionError("request headers must never be read")

    entry = _entry(Trap())
    assert set(entry) == {
        "i", "delay", "floor", "ceiling", "status", "url",
        "content_type", "body", "streamed", "classified", "rule",
    }  # fmt: skip


def test_only_two_response_headers_are_allowlisted() -> None:
    resp = Resp()
    resp.headers["set-cookie"] = f"sessionid={SESSION_ID}"
    assert SESSION_ID not in json.dumps(_entry(resp))


# --- the ring buffer -----------------------------------------------------------------------------


def test_the_buffer_is_bounded_and_keeps_the_newest() -> None:
    log = telemetry.RequestLog(size=3)
    for _ in range(10):
        log.record(Resp(), streamed=False, delay=1.0, floor=1.0, ceiling=2.0, classified="clean")
    entries = log.snapshot()
    assert len(entries) == 3
    assert [e["i"] for e in entries] == [7, 8, 9], "oldest first, newest kept"


def test_the_index_is_monotonic_across_evictions() -> None:
    log = telemetry.RequestLog(size=2)
    for _ in range(5):
        log.record(Resp(), streamed=False, delay=1.0, floor=1.0, ceiling=2.0, classified="clean")
    assert log.snapshot()[-1]["i"] == 4


def test_recording_never_raises_on_a_hostile_response() -> None:
    """Telemetry must not be able to cost a download."""

    class Hostile:
        @property
        def status_code(self) -> int:
            raise RuntimeError("boom")

    log = telemetry.RequestLog()
    log.record(Hostile(), streamed=False, delay=1.0, floor=1.0, ceiling=2.0, classified="clean")
    log.record(object(), streamed=False, delay=1.0, floor=1.0, ceiling=2.0, classified="clean")
    assert json.dumps(log.snapshot())  # whatever landed is serializable


def test_entries_are_json_native() -> None:
    entry = _entry(Resp(status=429), classified="http-429")
    assert json.loads(json.dumps(entry)) == entry
    assert entry["status"] == 429
    assert entry["classified"] == "http-429"
    assert entry["rule"] is None


# --- delivery: how the ring buffer leaves the worker ---------------------------------------------


def _worker_payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "job_id": "j1",
        "url": "https://www.instagram.com/p/abc/",
        "platform": "instagram",
        "output_dir": "/tmp/out",
        "cookies": {"sessionid": "SID"},
        "options": {},
        "pacing": {"mode": "adaptive", "min": 1.0, "max": 30.0},
    }
    base.update(over)
    return base


def _run_worker(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    job_body: Any,
    **payload_over: Any,
) -> list[dict[str, Any]]:
    from gallery_dl_web.gallerydl import worker

    monkeypatch.setattr(worker.config, "set", lambda *a: None)
    monkeypatch.setattr(worker.output, "initialize_logging", lambda lvl: None)
    monkeypatch.setattr(worker.job, "DownloadJob", job_body)
    worker.run(_worker_payload(**payload_over))
    out = capsys.readouterr().out.strip()
    return [json.loads(ln) for ln in out.splitlines() if ln.strip()]


def test_the_terminal_event_carries_the_ring_buffer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A post-mortem must be possible without reproducing the run."""

    def _job(url: str) -> Any:  # noqa: ARG001
        from gallery_dl_web.gallerydl import worker

        assert worker._PACER is not None
        worker._PACER.observe(Resp(status=429, body=b'{"status":"fail"}'))
        return _Status(0)

    evs = _run_worker(monkeypatch, capsys, _job)
    terminal = evs[-1]
    assert terminal["type"] == "completed"
    entries = terminal["pacing_telemetry"]
    assert len(entries) == 1
    assert entries[0]["status"] == 429
    assert entries[0]["classified"] == "http-429"


def test_a_crashing_job_still_carries_the_ring_buffer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _job(url: str) -> Any:  # noqa: ARG001
        from gallery_dl_web.gallerydl import worker

        assert worker._PACER is not None
        worker._PACER.observe(Resp(status=403))
        raise RuntimeError("boom")

    evs = _run_worker(monkeypatch, capsys, _job)
    assert evs[-1]["type"] == "failed"
    assert evs[-1]["reason"] == "worker-crash"
    assert evs[-1]["pacing_telemetry"][0]["status"] == 403


def test_no_telemetry_key_when_nothing_was_observed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An absent key reads better downstream than an empty list."""
    evs = _run_worker(monkeypatch, capsys, lambda url: _Status(0))  # noqa: ARG005
    assert "pacing_telemetry" not in evs[-1]


def test_fixed_mode_records_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No pacer is installed in fixed mode, so there is no capture — a known limitation."""
    evs = _run_worker(
        monkeypatch,
        capsys,
        lambda url: _Status(0),  # noqa: ARG005
        pacing={"mode": "fixed", "min": 1.0, "max": 3.0},
    )
    assert "pacing_telemetry" not in evs[-1]


def test_sigterm_flushes_a_non_terminal_event(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The stall-kill path is where the evidence matters most, and the worker never gets to emit
    its own terminal event there.

    It must NOT be a terminal event: the manager synthesizes its own on that path, and two would
    break the contract's "exactly one terminal event" rule.
    """
    import os
    import signal

    def _job(url: str) -> Any:  # noqa: ARG001
        from gallery_dl_web.gallerydl import worker

        assert worker._PACER is not None
        worker._PACER.observe(Resp(status=429))
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM handler should have raised SystemExit")

    evs = _run_worker(monkeypatch, capsys, _job)
    flushed = [e for e in evs if e["type"] == "pacing-telemetry"]
    assert len(flushed) == 1
    assert flushed[0]["reason"] == "terminated"
    assert flushed[0]["pacing_telemetry"][0]["status"] == 429
    assert flushed[0]["type"] not in ("completed", "failed", "cancelled")


def test_the_sigterm_handler_is_restored_after_the_job(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A leaked handler would outlive the job and fire against a dead pacer."""
    import signal

    before = signal.getsignal(signal.SIGTERM)
    _run_worker(monkeypatch, capsys, lambda url: _Status(0))  # noqa: ARG005
    assert signal.getsignal(signal.SIGTERM) is before


class _Status:
    """Minimal fake DownloadJob whose run() returns a gallery-dl status bitmask."""

    def __init__(self, status: int) -> None:
        self._status = status

    def run(self) -> int:
        return self._status
