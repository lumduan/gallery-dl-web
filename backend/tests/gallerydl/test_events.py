from __future__ import annotations

import json

import pytest

from gallery_dl_web.gallerydl import events


def test_emit_writes_one_json_line(capsys: pytest.CaptureFixture[str]) -> None:
    events.emit({"type": "x", "a": 1})
    line = capsys.readouterr().out.strip()
    assert json.loads(line) == {"type": "x", "a": 1}


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (0, ("completed", "ok")),
        (64, ("failed", "no-extractor")),
        (128, ("failed", "os-error")),
        (4, ("failed", "dl-failed")),
        (1, ("failed", "error")),
        (8, ("completed", "all-skipped")),
        (68, ("failed", "no-extractor")),  # 64 | 4
        (9, ("failed", "error")),  # 8 | 1
        (16, ("failed", "unknown-16")),  # no recognized bits set
    ],
)
def test_map_exit_status(status: int, expected: tuple[str, str]) -> None:
    assert events.map_exit_status(status) == expected
