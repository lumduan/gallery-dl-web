"""Health endpoint — returns ``{"status": "ok"}`` only. Leaks no cookie/version/count info."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
