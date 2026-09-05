"""File routes — browse and download files under the downloads directory.

Every path parameter is resolved and confined to ``downloads_dir`` to prevent traversal
(``..`` or absolute paths). Archive SQLite files are hidden from the listing.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from gallery_dl_web.api.deps import get_file_index, get_settings
from gallery_dl_web.config import Settings
from gallery_dl_web.files.index import FileIndex
from gallery_dl_web.schemas.files import FileListResponse

router = APIRouter(prefix="/files", tags=["files"])

# The page shows a screenful; the tree can hold hundreds of thousands. Serialising all of them was
# tens of megabytes of JSON per request on top of the walk itself.
DEFAULT_LIMIT = 1000
MAX_LIMIT = 5000


def _resolve_within(base: Path, rel: str) -> Path:
    from gallery_dl_web.api.paths import resolve_within

    try:
        return resolve_within(base, rel)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid path") from exc


@router.get("", response_model=FileListResponse)
async def list_files(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    index: FileIndex = Depends(get_file_index),
) -> FileListResponse:
    """The newest ``limit`` files, plus the real total.

    The walk itself runs in a thread behind a short-lived cache (``files/index.py``). Doing it
    inline here is what took the whole API down on a NAS-backed install: an event loop blocked in
    ``os.walk`` serves nothing at all, `/health` included.
    """
    entries = await index.entries()
    return FileListResponse(
        files=entries[:limit], total=len(entries), truncated=len(entries) > limit
    )


@router.get("/download")
async def download_file(
    path: str,
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    full = _resolve_within(settings.downloads_dir, path)
    if not full.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(full, filename=full.name)
