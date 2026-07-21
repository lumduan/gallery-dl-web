from __future__ import annotations

from pydantic import BaseModel


class FileEntry(BaseModel):
    path: str
    name: str
    size: int
    mtime: float
    platform: str


class FileListResponse(BaseModel):
    files: list[FileEntry]
