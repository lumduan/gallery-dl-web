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
    assert r.json() == {"has_ig": False, "has_fb": False}


async def test_put_ig_cookie(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        r = await client.put("/api/settings/cookies", json={"ig_sessionid": "SID123"})
        assert r.status_code == 200
        assert r.json() == {"has_ig": True, "has_fb": False}

        # Values are never returned — only booleans.
        r2 = await client.get("/api/settings")
        assert r2.json() == {"has_ig": True, "has_fb": False}


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
