from __future__ import annotations

import io
import json
from typing import Any

import pytest

from gallery_dl_web.gallerydl import worker


def _payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "job_id": "j1",
        "url": "https://www.instagram.com/p/abc/",
        "platform": "instagram",
        "output_dir": "/tmp/out",
        "cookies": {"sessionid": "SID"},
        "options": {},
    }
    base.update(over)
    return base


class _StatusJob:
    """Minimal fake DownloadJob: run() returns a chosen gallery-dl status bitmask."""

    def __init__(self, status: int) -> None:
        self._status = status

    def run(self) -> int:
        return self._status


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker.config, "set", lambda *a: None)
    monkeypatch.setattr(worker.output, "initialize_logging", lambda lvl: None)


def _events(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out.strip()
    return [json.loads(ln) for ln in out.splitlines() if ln.strip()]


def _reset_ctx() -> None:
    worker._CTX.update(job_id="j1", url="https://x/", downloaded=0, skipped=0, failed=0)


def test_started_then_completed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(worker.job, "DownloadJob", lambda url: _StatusJob(0))
    rc = worker.run(_payload())
    assert rc == 0
    evs = _events(capsys)
    assert [e["type"] for e in evs] == ["started", "completed"]
    assert evs[1]["reason"] == "ok"


def test_no_extractor_status_maps_to_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(worker.job, "DownloadJob", lambda url: _StatusJob(64))
    worker.run(_payload())
    evs = _events(capsys)
    assert evs[-1]["type"] == "failed"
    assert evs[-1]["reason"] == "no-extractor"


def test_on_file_downloaded(capsys: pytest.CaptureFixture[str], tmp_path) -> None:
    _reset_ctx()
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 42)
    worker.on_file({"_path": str(f), "filename": "a.jpg"})
    evs = _events(capsys)
    assert [e["type"] for e in evs] == ["file", "progress"]
    assert evs[0]["event"] == "downloaded"
    assert evs[0]["bytes"] == 42
    assert evs[0]["filename"] == "a.jpg"
    assert evs[1]["downloaded"] == 1 and evs[1]["skipped"] == 0


def test_on_skip_emits_skipped_and_progress(capsys: pytest.CaptureFixture[str]) -> None:
    _reset_ctx()
    worker.on_file({"_path": "/x/a.jpg", "filename": "a.jpg"})  # downloaded=1 first
    capsys.readouterr()
    worker.on_skip({"_path": "/x/b.jpg", "filename": "b.jpg"})
    evs = _events(capsys)
    assert evs[0]["type"] == "file" and evs[0]["event"] == "skipped"
    assert evs[1]["downloaded"] == 1 and evs[1]["skipped"] == 1


def test_on_prepare_emits_filename(capsys: pytest.CaptureFixture[str]) -> None:
    _reset_ctx()
    worker.on_prepare({"filename": "c.jpg"})
    evs = _events(capsys)
    assert evs[0]["type"] == "prepare"
    assert evs[0]["filename"] == "c.jpg"


def test_config_error_emits_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_common(monkeypatch)
    rc = worker.run(_payload(cookies={}, platform="instagram"))
    assert rc == 2
    evs = _events(capsys)
    assert evs[-1]["type"] == "failed"
    assert evs[-1]["reason"] == "worker-crash"
    assert "sessionid" in evs[-1]["message"]


def test_preview_uses_datajob(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, object] = {}

    def fake_download(url: str) -> _StatusJob:
        called["dj"] = True
        return _StatusJob(0)

    def fake_data(url: str) -> _StatusJob:
        called["data"] = True
        return _StatusJob(0)

    _patch_common(monkeypatch)
    monkeypatch.setattr(worker.job, "DownloadJob", fake_download)
    monkeypatch.setattr(worker.job, "DataJob", fake_data)
    worker.run(_payload(preview=True))
    assert "data" in called and "dj" not in called


def test_main_bad_stdin(capsys: pytest.CaptureFixture[str]) -> None:
    rc = worker.main(io.StringIO("not json"))
    assert rc == 2
    evs = _events(capsys)
    assert evs[-1]["reason"] == "bad-stdin"
