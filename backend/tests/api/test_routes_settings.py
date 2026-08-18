from __future__ import annotations

import httpx
from fastapi import FastAPI

from tests.conftest import FB_NETSCAPE


async def test_get_empty_settings(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert (body["has_ig"], body["has_fb"]) == (False, False)
    # Pacing rides along so the Settings page needs one request, not two.
    assert body["pacing"]["facebook"] == {
        "mode": "adaptive",
        "min": 1.0,
        "max": 30.0,
        "overridden": False,
    }


async def test_put_ig_cookie(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.put("/api/settings/cookies", json={"ig_sessionid": "SID123"})
        assert r.status_code == 200
        assert (r.json()["has_ig"], r.json()["has_fb"]) == (True, False)

        # Values are never returned — only booleans.
        r2 = await client.get("/api/settings")
        assert (r2.json()["has_ig"], r2.json()["has_fb"]) == (True, False)
        assert "SID123" not in r2.text


async def test_put_fb_cookies(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.put("/api/settings/cookies", json={"fb_cookies_text": FB_NETSCAPE})
        assert r.status_code == 200
        assert r.json()["has_fb"] is True


async def test_put_fb_invalid_returns_422(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.put("/api/settings/cookies", json={"fb_cookies_text": "garbage no tabs"})
    assert r.status_code == 422
    # The error must not echo a cookie value.
    assert "sessionid" not in r.text.lower()


async def test_put_pacing_overrides_the_env_default(app: FastAPI) -> None:
    """The override applies to the next job with no restart — that is the point of the store."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.put(
            "/api/settings/pacing",
            json={"platform": "facebook", "pacing": {"mode": "fixed", "min": 3, "max": 8}},
        )
        assert r.status_code == 200
        assert r.json()["pacing"]["facebook"] == {
            "mode": "fixed",
            "min": 3.0,
            "max": 8.0,
            "overridden": True,
        }
        # Instagram is untouched — the override is per platform.
        assert r.json()["pacing"]["instagram"]["overridden"] is False

    mgr = app.state.job_manager
    assert mgr._pacing_store.get("facebook") == {"mode": "fixed", "min": 3.0, "max": 8.0}


async def test_null_pacing_resets_to_the_env_default(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        await client.put(
            "/api/settings/pacing",
            json={"platform": "facebook", "pacing": {"mode": "fixed", "min": 3, "max": 8}},
        )
        r = await client.put("/api/settings/pacing", json={"platform": "facebook"})
        assert r.status_code == 200
        assert r.json()["pacing"]["facebook"] == {
            "mode": "adaptive",
            "min": 1.0,
            "max": 30.0,
            "overridden": False,
        }


async def test_pacing_rejects_an_unknown_platform_or_mode(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.put(
            "/api/settings/pacing",
            json={"platform": "tiktok", "pacing": {"mode": "fixed", "min": 1, "max": 2}},
        )
        assert r.status_code == 422
        r = await client.put(
            "/api/settings/pacing",
            json={"platform": "facebook", "pacing": {"mode": "warp", "min": 1, "max": 2}},
        )
        assert r.status_code == 422
        r = await client.put(
            "/api/settings/pacing",
            json={"platform": "facebook", "pacing": {"mode": "fixed", "min": -1, "max": 2}},
        )
        assert r.status_code == 422


async def test_pacing_survives_a_restart(app: FastAPI) -> None:
    """A rate-limit response is worthless if a container recreate silently undoes it."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        await client.put(
            "/api/settings/pacing",
            json={"platform": "instagram", "pacing": {"mode": "fixed", "min": 20, "max": 40}},
        )
    store = app.state.pacing_store
    reloaded = type(store)(store.path, app.state.settings)
    reloaded.load()
    assert reloaded.get("instagram") == {"mode": "fixed", "min": 20.0, "max": 40.0}


async def test_a_corrupt_pacing_file_falls_back_to_the_env(app: FastAPI) -> None:
    store = app.state.pacing_store
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    store.load()
    assert store.get("facebook") is None
    assert store.effective("facebook") == {"mode": "adaptive", "min": 1.0, "max": 30.0}
