"""File routes — browse and download files under the downloads directory.

Every path parameter is resolved and confined to ``downloads_dir`` to prevent traversal
(``..`` or absolute paths). Archive SQLite files are hidden from the listing.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from gallery_dl_web.api.deps import get_settings
from gallery_dl_web.config import Settings
from gallery_dl_web.schemas.files import FileEntry, FileListResponse

router = APIRouter(prefix="/files", tags=["files"])

_HIDDEN_SUFFIXES = (".sqlite", ".sqlite-journal", ".sqlite-wal", ".sqlite-shm", ".db")


def _resolve_within(base: Path, rel: str) -> Path:
    candidate = (base / rel).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid path") from exc
    return candidate


@router.get("", response_model=FileListResponse)
async def list_files(settings: Settings = Depends(get_settings)) -> FileListResponse:
    downloads = settings.downloads_dir
    entries: list[FileEntry] = []
    if downloads.exists():
        for root, _dirs, files in os.walk(downloads):
            for name in files:
                if name.lower().endswith(_HIDDEN_SUFFIXES):
                    continue
                full = Path(root) / name
                try:
                    st = full.stat()
                except OSError:
                    continue
                rel = full.relative_to(downloads)
                entries.append(
                    FileEntry(
                        path=str(rel),
                        name=name,
                        size=st.st_size,
                        mtime=st.st_mtime,
                        platform=rel.parts[0] if rel.parts else "",
                    )
                )
    entries.sort(key=lambda e: e.mtime, reverse=True)
    return FileListResponse(files=entries)


@router.get("/download")
async def download_file(
    path: str,
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    full = _resolve_within(settings.downloads_dir, path)
    if not full.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(full, filename=full.name)
