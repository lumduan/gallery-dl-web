"""Per-profile username extraction from Instagram/Facebook URLs + safe-name validation.

Used to name per-profile archives / metadata so a profile can be deleted + re-downloaded cleanly.
Returns None for URL shapes that don't expose a username (IG /p/<shortcode>/, FB /photo/?fbid=...);
callers fall back to a shared per-platform archive for those.
"""

from __future__ import annotations

import re

_FORBIDDEN_NAMES = {".", ".."}


def is_safe_profile_name(name: str) -> bool:
    """True if ``name`` is safe as a single path segment.

    gallery-dl uses the profile's DISPLAY NAME as the {username} folder, which can contain spaces,
    dots, and unicode — so we only reject real traversal vectors (path separators, NUL, "." / "..").
    ``resolve_within()`` is the hard guard on every file access anyway.
    """
    if not name or name in _FORBIDDEN_NAMES:
        return False
    return "/" not in name and "\\" not in name and "\x00" not in name


# Instagram: stories/ is matched by its own regex (different shape); the username regex uses a
# negative lookahead to skip single-media paths (/p/, /reel/, /reels/, /explore/, ...).
_IG_USERNAME_RE = re.compile(
    r"instagram\.com/(?!p/|reel/|reels/|stories/|"
    r"explore/|accounts/|direct/)([A-Za-z0-9._]{1,30})/?",
    re.IGNORECASE,
)
_IG_STORIES_RE = re.compile(r"instagram\.com/stories/([A-Za-z0-9._]{1,30})/", re.IGNORECASE)

# Facebook: profile.php?id=N, groups/<g>/, or <username> (skipping photo/, watch/, permalink, ...).
_FB_PROFILE_ID_RE = re.compile(r"facebook\.com/profile\.php\?id=(\d+)", re.IGNORECASE)
_FB_GROUP_RE = re.compile(
    r"(?:facebook\.com|fb\.com)/groups/([A-Za-z0-9.\-]{1,50})/", re.IGNORECASE
)
_FB_USERNAME_RE = re.compile(
    r"(?:facebook\.com|fb\.com|fb\.watch)/"
    r"(?!photo[/?]|watch[/?]|permalink\.php|sharer|login|groups/)"
    r"([A-Za-z0-9.\-]{1,50})/?",
    re.IGNORECASE,
)


def extract_username(url: str, platform: str) -> str | None:
    """Best-effort profile key for per-profile archive naming. None if not derivable."""
    if platform == "instagram":
        m = _IG_STORIES_RE.search(url) or _IG_USERNAME_RE.search(url)
        return m.group(1) if m else None
    if platform == "facebook":
        mid = _FB_PROFILE_ID_RE.search(url)
        if mid:
            return f"id_{mid.group(1)}"
        mg = _FB_GROUP_RE.search(url)
        if mg:
            return mg.group(1)
        mu = _FB_USERNAME_RE.search(url)
        return mu.group(1) if mu else None
    return None
