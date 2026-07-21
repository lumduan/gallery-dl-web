from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from gallery_dl_web.api.app import create_app
from gallery_dl_web.config import Settings


def test_create_app_and_lifespan(tmp_path: Path) -> None:
    data = tmp_path / "data"
    settings = Settings(
        data_dir=data,
        downloads_dir=data / "downloads",
        cookies_path=data / "cookies.json",
        cors_origins=["http://x"],
        max_concurrent_jobs=1,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        # Lifespan ran: download + archive dirs created.
        assert settings.downloads_dir.exists()
        assert (settings.data_dir / "archive").exists()


def test_create_app_isolates_state(tmp_path: Path) -> None:
    data1 = tmp_path / "d1"
    data2 = tmp_path / "d2"
    app1 = create_app(
        Settings(data_dir=data1, downloads_dir=data1 / "dl", cookies_path=data1 / "c.json")
    )
    app2 = create_app(
        Settings(data_dir=data2, downloads_dir=data2 / "dl", cookies_path=data2 / "c.json")
    )
    assert app1.state.cookie_store is not app2.state.cookie_store
    assert app1.state.job_manager is not app2.state.job_manager
