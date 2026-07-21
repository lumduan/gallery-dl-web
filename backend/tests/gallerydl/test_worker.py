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


class FaithfulFakeJob:
    """Mimics gallery_dl.job.DownloadJob's hook lifecycle faithfully.

    gallery-dl leaves ``self.hooks`` as an empty TUPLE after __init__; it only becomes a
    defaultdict(list) inside initialize(). ``register_hooks`` indexes hooks by event name, so it
    raises TypeError if hooks is still a tuple. This fake reproduces that, so the worker MUST set
    ``j.hooks = defaultdict(list)`` before registering — exactly the bug we fixed.
    """

    def __init__(self, fire: list[str], status: int, pathfmt: Any) -> None:
        self.hooks: Any = ()  # tuple, like the real job pre-initialize
        self._fire = fire
        self._status = status
        self._pf = pathfmt

    def register_hooks(self, hooks: dict[str, Any]) -> None:
        # Identical indexing pattern to gallery_dl.job.DownloadJob.register_hooks.
        for hook, callback in hooks.items():
            self.hooks[hook].append(callback)

    def run(self) -> int:
        for event in self._fire:
            for cb in self.hooks.get(event, []):
                if event == "error":
                    cb(self._pf, ValueError("boom"))
                else:
                    cb(self._pf)
        return self._status


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

    def make_job(url: str) -> FaithfulFakeJob:
        return FaithfulFakeJob(fire=["prepare", "file"], status=0, pathfmt=pf)

    monkeypatch.setattr(worker.job, "DownloadJob", make_job)
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

    monkeypatch.setattr(
        worker.job, "DownloadJob", lambda url: FaithfulFakeJob(["skip"], 8, pf)
    )
    _patch_common(monkeypatch)

    rc = worker.run(_payload())
    assert rc == 0
    evs = _events(capsys)
    assert evs[-1]["type"] == "completed"
    assert evs[-1]["reason"] == "all-skipped"
    assert any(e["type"] == "file" and e["event"] == "skipped" for e in evs)


def test_no_extractor_status64(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    pf = FakePF("x", str(tmp_path / "x"))
    monkeypatch.setattr(
        worker.job, "DownloadJob", lambda url: FaithfulFakeJob([], 64, pf)
    )
    _patch_common(monkeypatch)

    rc = worker.run(_payload())
    assert rc == 0
    evs = _events(capsys)
    assert evs[-1]["type"] == "failed"
    assert evs[-1]["reason"] == "no-extractor"


def test_error_hook_emits_nonfatal_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    pf = FakePF("e.jpg", str(tmp_path / "e.jpg"))
    monkeypatch.setattr(
        worker.job, "DownloadJob", lambda url: FaithfulFakeJob(["error"], 0, pf)
    )
    _patch_common(monkeypatch)

    worker.run(_payload())
    evs = _events(capsys)
    err = next(e for e in evs if e["type"] == "error")
    assert err["fatal"] is False
    assert err["message"] == "boom"


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
