from __future__ import annotations

from pydantic import BaseModel, Field


class SettingsResponse(BaseModel):
    has_ig: bool
    has_fb: bool


class CookiesUpdateRequest(BaseModel):
    ig_sessionid: str | None = Field(
        default=None, description="Instagram sessionid cookie. Empty string clears it."
    )
    fb_cookies_text: str | None = Field(
        default=None,
        description="Facebook cookies in Netscape cookies.txt format. Empty string clears it.",
    )
