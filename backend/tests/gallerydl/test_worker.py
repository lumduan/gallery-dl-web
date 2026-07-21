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


class FakePF:
    def __init__(self, filename: str = "f.jpg", path: str = "/tmp/out/f.jpg") -> None:
        self.filename = filename
        self.path = path


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker.config, "set", lambda *a: None)
    monkeypatch.setattr(worker.output, "initialize_logging", lambda lvl: None)


def _events(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out.strip()
    return [json.loads(ln) for ln in out.splitlines() if ln.strip()]


def test_success_emits_started_file_completed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    f = tmp_path / "f.jpg"
    f.write_bytes(b"x" * 42)
    pf = FakePF("f.jpg", str(f))

    class FakeJob:
        def __init__(self, url: str) -> None:
            self.url = url
            self.hooks: dict[str, Any] = {}

        def register_hooks(self, hooks: dict[str, Any]) -> None:
            self.hooks.update(hooks)

        def run(self) -> int:
            self.hooks["prepare"](pf)
            self.hooks["file"](pf)
            return 0

    monkeypatch.setattr(worker.job, "DownloadJob", FakeJob)
    _patch_common(monkeypatch)

    rc = worker.run(_payload())
    assert rc == 0

    evs = _events(capsys)
    types = [e["type"] for e in evs]
    assert types[0] == "started"
    assert types[-1] == "completed"
    file_ev = next(e for e in evs if e["type"] == "file")
    assert file_ev["event"] == "downloaded"
    assert file_ev["bytes"] == 42
    assert file_ev["filename"] == "f.jpg"


def test_all_skipped_status8_is_completed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    pf = FakePF("s.jpg", str(tmp_path / "s.jpg"))

    class FakeJob:
        def __init__(self, url: str) -> None:
            self.hooks: dict[str, Any] = {}

        def register_hooks(self, hooks: dict[str, Any]) -> None:
            self.hooks.update(hooks)

        def run(self) -> int:
            self.hooks["skip"](pf)
            return 8

    monkeypatch.setattr(worker.job, "DownloadJob", FakeJob)
    _patch_common(monkeypatch)

    rc = worker.run(_payload())
    assert rc == 0
    evs = _events(capsys)
    assert evs[-1]["type"] == "completed"
    assert evs[-1]["reason"] == "all-skipped"
    assert any(e["type"] == "file" and e["event"] == "skipped" for e in evs)


def test_no_extractor_status64(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeJob:
        def __init__(self, url: str) -> None:
            pass

        def register_hooks(self, hooks: dict[str, Any]) -> None:
            pass

        def run(self) -> int:
            return 64

    monkeypatch.setattr(worker.job, "DownloadJob", FakeJob)
    _patch_common(monkeypatch)

    rc = worker.run(_payload())
    assert rc == 0
    evs = _events(capsys)
    assert evs[-1]["type"] == "failed"
    assert evs[-1]["reason"] == "no-extractor"


def test_config_error_emits_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_common(monkeypatch)
    # No sessionid -> config_builder raises -> worker emits failed.
    rc = worker.run(_payload(cookies={}, platform="instagram"))
    assert rc == 2
    evs = _events(capsys)
    assert evs[-1]["type"] == "failed"
    assert evs[-1]["reason"] == "worker-crash"
    assert "sessionid" in evs[-1]["message"]


def test_preview_uses_datajob(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called: dict[str, Any] = {}

    class FakeDJ:
        def __init__(self, url: str) -> None:
            called["dj"] = self

        def register_hooks(self, hooks: dict[str, Any]) -> None:
            self.hooks = hooks

        def run(self) -> int:
            return 0

    class FakeDataJob:
        def __init__(self, url: str) -> None:
            called["data"] = self
            self.hooks: dict[str, Any] = {}

        def register_hooks(self, hooks: dict[str, Any]) -> None:
            self.hooks = hooks

        def run(self) -> int:
            return 0

    monkeypatch.setattr(worker.job, "DownloadJob", FakeDJ)
    monkeypatch.setattr(worker.job, "DataJob", FakeDataJob)
    _patch_common(monkeypatch)

    worker.run(_payload(preview=True))
    assert "data" in called and "dj" not in called


def test_main_bad_stdin(capsys: pytest.CaptureFixture[str]) -> None:
    rc = worker.main(io.StringIO("not json"))
    assert rc == 2
    evs = _events(capsys)
    assert evs[-1]["reason"] == "bad-stdin"
