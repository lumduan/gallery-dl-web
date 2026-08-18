"""Settings routes — cookies and download pacing.

Cookie values are never returned, only boolean presence. Pacing is ordinary configuration, so it
*is* returned, and it is read at job-build time rather than at startup — after a rate-limit block
the operator needs to slow a platform down now, not after a container recreate.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from gallery_dl_web.api.deps import get_cookie_store, get_pacing_store
from gallery_dl_web.cookies.store import CookieStore
from gallery_dl_web.pacing.store import PacingStore
from gallery_dl_web.schemas.settings import (
    CookiesUpdateRequest,
    PacingUpdateRequest,
    SettingsResponse,
)

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=SettingsResponse)
async def get_settings(
    cs: CookieStore = Depends(get_cookie_store),
    ps: PacingStore = Depends(get_pacing_store),
) -> SettingsResponse:
    return SettingsResponse(**cs.status(), pacing=ps.status())


@router.put("/cookies", response_model=SettingsResponse)
async def update_cookies(
    req: CookiesUpdateRequest,
    cs: CookieStore = Depends(get_cookie_store),
    ps: PacingStore = Depends(get_pacing_store),
) -> SettingsResponse:
    try:
        cs.update(ig_sessionid=req.ig_sessionid, fb_cookies_text=req.fb_cookies_text)
    except ValueError as exc:
        # Never echo the submitted value back — name the field/problem only.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SettingsResponse(**cs.status(), pacing=ps.status())


@router.put("/pacing", response_model=SettingsResponse)
async def update_pacing(
    req: PacingUpdateRequest,
    cs: CookieStore = Depends(get_cookie_store),
    ps: PacingStore = Depends(get_pacing_store),
) -> SettingsResponse:
    """Set or clear one platform's pacing override. Applies to the next job, with no restart."""
    try:
        ps.update(req.platform, req.pacing.model_dump() if req.pacing else None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SettingsResponse(**cs.status(), pacing=ps.status())
