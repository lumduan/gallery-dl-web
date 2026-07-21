"""Job routes: create, list, inspect, stream progress over SSE, download a job's files as zip."""

from __future__ import annotations

import asyncio
import json
import tempfile
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from gallery_dl_web.api.deps import get_job_manager, get_settings
from gallery_dl_web.config import Settings
from gallery_dl_web.jobs.manager import JobManager
from gallery_dl_web.schemas.jobs import JobCreateRequest, JobResponse, JobSummary

router = APIRouter(tags=["jobs"])

_TERMINAL = frozenset({"completed", "failed"})


def detect_platform(url: str) -> str | None:
    u = url.lower()
    if "instagram.com" in u:
        return "instagram"
    if "facebook.com" in u or "fb.com" in u or "fb.watch" in u:
        return "facebook"
    return None


@router.post("/jobs", response_model=JobResponse, status_code=202)
async def create_job(
    req: JobCreateRequest,
    mgr: JobManager = Depends(get_job_manager),
) -> JobResponse:
    platform = req.platform or detect_platform(req.url)
    if platform is None:
        raise HTTPException(
            status_code=400,
            detail="Could not detect platform from URL (expected instagram.com or facebook.com).",
        )
    job_id = await mgr.create_job(req.url, platform, req.options)
    return JobResponse(job_id=job_id, status="queued")


@router.get("/jobs", response_model=list[JobSummary])
async def list_jobs(mgr: JobManager = Depends(get_job_manager)) -> list[JobSummary]:
    return [JobSummary(**s.to_summary()) for s in mgr.list_jobs()]


@router.get("/jobs/{job_id}", response_model=JobSummary)
async def get_job(
    job_id: str,
    mgr: JobManager = Depends(get_job_manager),
) -> JobSummary:
    state = mgr.get(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobSummary(**state.to_summary())


@router.get("/jobs/{job_id}/events")
async def job_events(
    job_id: str,
    request: Request,
    mgr: JobManager = Depends(get_job_manager),
) -> EventSourceResponse:
    state = mgr.get(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="job not found")

    async def event_generator() -> AsyncIterator[dict[str, Any]]:
        # 1. Replay bounded history so a reconnecting client sees everything it missed.
        for past in list(state.events):
            yield {"event": str(past.get("type", "message")), "data": json.dumps(past)}
        if state.is_terminal:
            yield {"event": "end", "data": json.dumps({"terminal": True})}
            return
        # 2. Subscribe to live events.
        queue = mgr.subscribe(state)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"event": str(event.get("type", "message")), "data": json.dumps(event)}
                if event.get("type") in _TERMINAL:
                    yield {"event": "end", "data": json.dumps({"terminal": True})}
                    break
        finally:
            mgr.unsubscribe(state, queue)

    return EventSourceResponse(
        event_generator(),
        ping=15,
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


def _resolve_within(base: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``base`` and reject path traversal.

    Raises HTTPException on violation.
    """
    candidate = (base / rel).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid path") from exc
    return candidate


@router.get("/jobs/{job_id}/zip")
async def download_job_zip(
    job_id: str,
    mgr: JobManager = Depends(get_job_manager),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    state = mgr.get(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="job not found")
    paths = state.downloaded_paths()
    if not paths:
        raise HTTPException(status_code=404, detail="job has no downloaded files")

    # Validate every path is inside downloads_dir, then zip into a temp file.
    downloads = settings.downloads_dir.resolve()
    safe: list[Path] = []
    for raw in paths:
        resolved = Path(raw).resolve()
        try:
            resolved.relative_to(downloads)
        except ValueError:
            continue
        if resolved.is_file():
            safe.append(resolved)
    if not safe:
        raise HTTPException(status_code=404, detail="job has no downloadable files")

    with (
        tempfile.NamedTemporaryFile(prefix=f"job-{job_id}-", suffix=".zip", delete=False) as tmp,
        zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf,
    ):
        for resolved in safe:
            arcname = resolved.relative_to(downloads)
            zf.write(resolved, arcname)

    return FileResponse(
        tmp.name,
        media_type="application/zip",
        filename=f"job-{job_id}.zip",
    )
