"""Per-platform pacing overrides an operator can change without restarting the container.

``Settings`` reads pacing from the environment, which is the right default source but the wrong
place to react from: after a block you want to slow Facebook down *now*, and an env change needs a
container recreate. So this store sits one layer above it — a small JSON file in the data volume,
read at job-build time, holding only what the operator explicitly set.

Deliberately not merged into ``CookieStore``: cookies are credentials (0600, never echoed back,
never logged) and pacing is plain configuration the UI must be able to read back and display.
Keeping them in separate files keeps that asymmetry obvious.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from gallery_dl_web.config import Settings, normalize_pacing

logger = logging.getLogger(__name__)

PLATFORMS = ("instagram", "facebook")


class PacingStore:
    """Operator overrides for per-platform pacing, backed by a JSON file."""

    def __init__(self, path: Path, settings: Settings) -> None:
        self._path = path
        self._settings = settings
        self._data: dict[str, Any] = {}

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        if not self._path.exists():
            self._data = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._data = raw if isinstance(raw, dict) else {}
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt override file must not take the app down — fall back to the env defaults.
            logger.error("failed to read pacing store at %s: %s", self._path, exc)
            self._data = {}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data), encoding="utf-8")

    def get(self, platform: str) -> dict[str, Any] | None:
        """The operator's override for one platform, or None if they have not set one."""
        return normalize_pacing(self._data.get(platform))

    def effective(self, platform: str) -> dict[str, Any] | None:
        """What a job on this platform would actually use: the override, else the env default."""
        return self.get(platform) or self._settings.pacing_for(platform)

    def status(self) -> dict[str, Any]:
        """What the Settings UI renders: the effective values plus whether each is an override.

        ``overridden`` is what lets the UI offer "reset to default" honestly — without it the
        operator cannot tell a value they set from one that came from the environment.
        """
        out: dict[str, Any] = {}
        for platform in PLATFORMS:
            effective = self.effective(platform)
            if effective is None:
                continue
            out[platform] = {**effective, "overridden": self.get(platform) is not None}
        return out

    def update(self, platform: str, pacing: Any) -> None:
        """Set or clear one platform's override. ``None`` clears it (back to the env default)."""
        if platform not in PLATFORMS:
            raise ValueError(f"unsupported platform: {platform!r}")
        if pacing is None:
            self._data.pop(platform, None)
        else:
            normalized = normalize_pacing(pacing)
            if normalized is None:
                raise ValueError(
                    "pacing must be {'mode': 'adaptive'|'fixed', 'min': number, 'max': number}"
                )
            self._data[platform] = normalized
        self.save()
