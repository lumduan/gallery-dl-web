"""Single-account cookie store.

Cookies are the ONLY way to authenticate IG/FB in gallery-dl (password login is disabled for
Instagram). They are full-session credentials — especially Facebook (``c_user`` + ``xs`` grant
full account access) — so this store keeps them in a single gitignored, mode-0600 JSON file
inside the ``/data`` volume. They are NEVER placed in argv, env vars, logs, or API responses
(``GET /api/settings`` returns only boolean presence flags).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# FB cookie names that indicate a real logged-in session.
_FB_SESSION_NAMES = {"c_user", "xs"}


def parse_netscape(text: str) -> dict[str, str]:
    """Parse Netscape ``cookies.txt`` text into a ``{name: value}`` dict.

    Each data line is 7+ tab-separated fields: ``domain  flag  path  secure  expiration
    name  value``. ``#HttpOnly_``-prefixed lines are data (not comments). Raises ValueError if no
    valid cookies.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and not stripped.startswith("#HttpOnly_"):
            continue
        parts = stripped.split("\t")
        if len(parts) < 7:
            continue
        name = parts[5].strip()
        value = parts[6].strip()
        if name:
            out[name] = value
    if not out:
        raise ValueError("no valid Netscape cookie lines found")
    return out


class CookieStore:
    """Persistent single-account cookie store backed by a JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, Any] = {}

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        if not self._path.exists():
            self._data = {}
            return
        try:
            self._data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(self._data, dict):
                self._data = {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("failed to read cookie store at %s: %s", self._path, exc)
            self._data = {}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data), encoding="utf-8")
        try:
            os.chmod(self._path, 0o600)
        except OSError as exc:
            logger.warning("could not chmod cookie store %s: %s", self._path, exc)

    def status(self) -> dict[str, bool]:
        return {"has_ig": self.has_ig(), "has_fb": self.has_fb()}

    def has_ig(self) -> bool:
        return bool(self._data.get("instagram", {}).get("sessionid"))

    def has_fb(self) -> bool:
        cookies = self._data.get("facebook", {}).get("cookies")
        if not cookies:
            return False
        if isinstance(cookies, dict):
            return bool(_FB_SESSION_NAMES & set(cookies.keys()))
        return True

    def get_for_platform(self, platform: str) -> dict[str, Any] | None:
        """Return the cookie payload for the worker, or None if not configured.

        IG -> ``{"sessionid": ...}``; FB -> ``{name: value, ...}``.
        """
        if platform == "instagram":
            sid = self._data.get("instagram", {}).get("sessionid")
            return {"sessionid": sid} if sid else None
        if platform == "facebook":
            cookies = self._data.get("facebook", {}).get("cookies")
            if isinstance(cookies, dict) and cookies:
                return cookies
            if isinstance(cookies, str) and cookies:
                return {"_file": cookies}
        return None

    def update(
        self,
        ig_sessionid: str | None = None,
        fb_cookies_text: str | None = None,
    ) -> None:
        """Update one or both platforms and persist. Empty strings clear a platform."""
        if ig_sessionid is not None:
            self._data.setdefault("instagram", {})["sessionid"] = ig_sessionid.strip()
        if fb_cookies_text is not None:
            text = fb_cookies_text.strip()
            if not text:
                self._data.setdefault("facebook", {})["cookies"] = {}
            else:
                parsed = parse_netscape(text)
                self._data.setdefault("facebook", {})["cookies"] = parsed
        self.save()

    @staticmethod
    def mask(value: str | None) -> str:
        """Mask a credential for safe logging: ``abcd…xy``."""
        if not value:
            return ""
        if len(value) <= 6:
            return "***"
        return f"{value[:4]}…{value[-2:]}"
