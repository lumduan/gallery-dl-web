from __future__ import annotations

from gallery_dl_web import __main__ as entrypoint


def test_main_invokes_uvicorn(monkeypatch) -> None:
    captured: dict = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(entrypoint.uvicorn, "run", fake_run)
    entrypoint.main()

    assert captured["kwargs"]["factory"] is True
    assert captured["kwargs"]["host"]
    assert isinstance(captured["kwargs"]["port"], int)
