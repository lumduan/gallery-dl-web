"""Application settings (pydantic-settings).

All paths default to an in-container ``/data`` layout that is bind-mounted (cookies.json +
downloads/ + archive/ persist in a named volume). Cookies are NEVER read from the environment;
they are managed at runtime via the Settings UI and stored only in ``cookies_path``.
"""

from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000

    data_dir: Path = Path("/data")
    downloads_dir: Path = Path("/data/downloads")
    cookies_path: Path = Path("/data/cookies.json")

    # NoDecode stops pydantic-settings from JSON-parsing the env var; the validator below splits it
    # as a comma-separated list. (Code callers may still pass a list directly.)
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    max_concurrent_jobs: int = 2

    # Interpreter used to spawn the per-job gallery-dl worker subprocess.
    # Empty string -> resolved to sys.executable at runtime.
    worker_python: str = ""

    # Profile-management knobs.
    zip_ttl_seconds: int = 300  # how long a generated profile .zip lives after last access
    thumbnail_size: int = 300  # longest-side px for generated thumbnails

    # Pausing SIGSTOPs the worker and hands its concurrency slot back, so a paused job is not
    # terminal and GC never reaps it — it would keep a suspended gallery-dl process, its memory and
    # an open archive SQLite handle alive forever. Auto-cancel after this long (0 disables).
    pause_max_seconds: float = 7200.0

    # Per-request pacing -> gallery-dl `sleep-request` (a [min, max] range it samples per request).
    # Both platforms rate-limit scraping. Facebook is the harsher one: with no delay it blocked an
    # account after ~767 images in a single run ("You've been temporarily blocked from viewing
    # images"), so it is paced too — less aggressively than Instagram, since Facebook needs a page
    # request per photo and the delay compounds. Raise these if you get blocked; a block costs far
    # more time than the delay does.
    instagram_sleep_request_min: float = 6.0
    instagram_sleep_request_max: float = 12.0
    facebook_sleep_request_min: float = 3.0
    facebook_sleep_request_max: float = 8.0

    def sleep_request_for(self, platform: str) -> list[float] | None:
        """The [min, max] sleep-request range for a platform, or None if unknown/disabled."""
        pairs = {
            "instagram": (self.instagram_sleep_request_min, self.instagram_sleep_request_max),
            "facebook": (self.facebook_sleep_request_min, self.facebook_sleep_request_max),
        }
        pair = pairs.get(platform)
        if pair is None:
            return None
        lo, hi = max(0.0, pair[0]), max(0.0, pair[1])
        if hi <= 0:
            return None  # explicitly disabled
        return [lo, max(lo, hi)]

    # Stall detection + retry. Two independent deadlines (see jobs/manager.py):
    #   * LIVENESS  — no line at all on worker stdout (not even a heartbeat) => the process is
    #     wedged (blocked pipe, deadlock). Short.
    #   * PROGRESS  — no *file* event. Before the first file this is the WARM-UP budget (gallery-dl
    #     is enumerating a profile and is legitimately silent for minutes — Instagram alone sleeps
    #     6-12s between requests). After the first file it is
    #     clamp(floor*backoff**attempt, multiplier*avg_inter_file, cap).
    http_timeout_seconds: float = 30.0  # gallery-dl extractor.timeout (per-request deadline)
    heartbeat_seconds: float = 15.0  # worker heartbeat interval (0 disables)
    stall_liveness_seconds: float = 60.0  # max silence incl. heartbeats before declaring a wedge
    stall_warmup_seconds: float = 600.0  # time-to-FIRST-file budget (extraction/enumeration)
    stall_warmup_max_retries: int = 1  # a warm-up timeout rarely benefits from more retries
    # Minimum steady-state threshold (after activity starts). Deliberately generous: a real wedge
    # is caught by the liveness check within stall_liveness_seconds, so the cost of a loose floor
    # is small, while a tight one kills healthy jobs (a live IG run went 90s between file events
    # while happily emitting `prepare`s).
    stall_floor_seconds: float = 300.0
    stall_multiplier: float = 4.0  # threshold scales with the running avg inter-file time
    stall_cap_seconds: float = 600.0  # max threshold
    stall_max_retries: int = 2  # retry a stalled download this many times before failing
    stall_backoff: float = 1.5  # threshold multiplied by this each retry attempt
    stall_kill_grace_seconds: float = 10.0  # SIGTERM -> SIGKILL grace when killing a stalled worker

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


def get_settings() -> Settings:
    """Factory (no caching) — callers create app.state singletons explicitly."""
    return Settings()
