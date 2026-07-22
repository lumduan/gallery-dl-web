"""Shared filesystem path helpers (path-traversal-safe resolution)."""

from __future__ import annotations

from pathlib import Path


def resolve_within(base: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``base`` and confirm it stays within ``base``.

    Raises ``ValueError`` on path traversal (so callers can map it to an HTTP error).
    """
    base_r = base.resolve()
    candidate = (base_r / rel).resolve()
    candidate.relative_to(base_r)  # raises ValueError if it escapes base
    return candidate
