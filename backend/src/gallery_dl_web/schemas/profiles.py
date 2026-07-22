from __future__ import annotations

from pydantic import BaseModel


class ProfileFileEntry(BaseModel):
    filename: str
    path: str
    bytes: int
    mtime: float
    kind: str  # "image" | "video"
    media_id: str | None = None
    thumb_url: str
    file_url: str


class ProfileMetadata(BaseModel):
    platform: str
    name: str
    avatar: str | None = None
    avatar_url: str | None = None
    images: list[ProfileFileEntry]
    videos: list[ProfileFileEntry]
    image_count: int
    video_count: int
    total_bytes: int
    last_updated: float


class ProfileSummary(BaseModel):
    platform: str
    name: str
    avatar_url: str | None = None
    image_count: int
    video_count: int
    total_bytes: int
    last_updated: float
