from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class JobCreateRequest(BaseModel):
    url: str = Field(..., min_length=1, description="Instagram or Facebook URL to download from.")
    platform: str | None = Field(
        default=None, description="Override platform; auto-detected from the URL if omitted."
    )
    options: dict[str, Any] | None = Field(
        default=None, description="gallery-dl options (include, videos, directory, filename, ...)."
    )


class JobResponse(BaseModel):
    job_id: str
    status: str


class JobSummary(BaseModel):
    id: str
    url: str
    platform: str
    # Profile display name for the queue UI: URL-derived at creation, replaced by gallery-dl's
    # actual folder name once a file lands. None for single-post URLs.
    profile: str | None = None
    status: str
    created_at: float
    started_at: float | None = None
    ended_at: float | None = None
    paused_at: float | None = None
    # Live counters, so the queue page can poll this one endpoint instead of an SSE per job.
    downloaded: int = 0
    skipped: int = 0
    file_count: int = 0
    last_activity_ts: float | None = None
    final_summary: dict[str, Any] | None = None
