"""Settings routes — cookie management. Never returns cookie values, only boolean presence."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from gallery_dl_web.api.deps import get_cookie_store
from gallery_dl_web.cookies.store import CookieStore
from gallery_dl_web.schemas.settings import CookiesUpdateRequest, SettingsResponse

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=SettingsResponse)
async def get_settings(cs: CookieStore = Depends(get_cookie_store)) -> SettingsResponse:
    return SettingsResponse(**cs.status())


@router.put("/cookies", response_model=SettingsResponse)
async def update_cookies(
    req: CookiesUpdateRequest,
    cs: CookieStore = Depends(get_cookie_store),
) -> SettingsResponse:
    try:
        cs.update(ig_sessionid=req.ig_sessionid, fb_cookies_text=req.fb_cookies_text)
    except ValueError as exc:
        # Never echo the submitted value back — name the field/problem only.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SettingsResponse(**cs.status())
