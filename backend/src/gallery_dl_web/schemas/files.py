from __future__ import annotations

from pydantic import BaseModel


class FileEntry(BaseModel):
    path: str
    name: str
    size: int
    mtime: float
    platform: str


class FileListResponse(BaseModel):
    """A *page* of the listing, newest first.

    `total` is the real number of files on disk, not `len(files)`. The two differ on any sizeable
    install — a real one holds 427,009 — and serialising all of them was tens of megabytes of JSON
    for a page that shows a screenful.
    """

    files: list[FileEntry]
    total: int = 0
    truncated: bool = False
