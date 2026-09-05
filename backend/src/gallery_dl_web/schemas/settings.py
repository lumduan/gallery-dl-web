from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PacingSettings(BaseModel):
    """Pacing for one platform, as the UI shows it: per-request (min/max) and per-image."""

    mode: Literal["adaptive", "fixed"] = Field(
        description=(
            "adaptive: `min` is the floor and starting delay, `max` the back-off ceiling — the "
            "worker starts fast and slows down only when the platform pushes back. "
            "fixed: every request sleeps a random value in [min, max]."
        )
    )
    min: float = Field(ge=0, description="Seconds. Floor (adaptive) or range low (fixed).")
    max: float = Field(ge=0, description="Seconds. Ceiling (adaptive) or range high (fixed).")
    per_file: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Seconds between individual image downloads, jittered +/-15%. A different axis from "
            "min/max, which space the extractor's API requests: one Instagram request returns ~30 "
            "posts whose images then download back to back. Applies in both modes. 0 disables it; "
            "null means 'not specified' and inherits the environment default."
        ),
    )


class SettingsResponse(BaseModel):
    has_ig: bool
    has_fb: bool
    # Per platform: the effective values plus `overridden`, so the UI can offer an honest
    # "reset to default" instead of guessing whether a value came from the operator or the env.
    pacing: dict[str, Any] = Field(default_factory=dict)


class PacingUpdateRequest(BaseModel):
    platform: Literal["instagram", "facebook"]
    pacing: PacingSettings | None = Field(
        default=None, description="Null clears the override, restoring the environment default."
    )


class CookiesUpdateRequest(BaseModel):
    ig_sessionid: str | None = Field(
        default=None, description="Instagram sessionid cookie. Empty string clears it."
    )
    fb_cookies_text: str | None = Field(
        default=None,
        description="Facebook cookies in Netscape cookies.txt format. Empty string clears it.",
    )
