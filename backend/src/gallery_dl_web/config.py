"""Application settings (pydantic-settings).

All paths default to an in-container ``/data`` layout that is bind-mounted (cookies.json +
downloads/ + archive/ persist in a named volume). Cookies are NEVER read from the environment;
they are managed at runtime via the Settings UI and stored only in ``cookies_path``.
"""

from pathlib import Path
from typing import Annotated, Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PACING_MODES = ("adaptive", "fixed")


# Largest per-file delay an operator can ask for. Mirrors ``pacing.HARD_MAX_DELAY``; the binding
# constraint is the stall detector, whose progress deadline is
# ``max(stall_floor_seconds, stall_multiplier * avg_inter_file)`` — at the 300 s / 4x defaults a
# 60 s per-file delay still leaves the deadline five times the gap it is measuring.
MAX_PER_FILE_DELAY = 60.0


def normalize_pacing(raw: Any) -> dict[str, Any] | None:
    """Coerce a pacing block into ``{"mode", "min", "max", "per_file"}``, or None if unusable.

    Shared by ``Settings``, the runtime store and the per-job option, so an operator, a JSON file
    and an API caller cannot disagree about what a valid value is. Anything unrecognised returns
    None, which means "this layer has no opinion" and lets the layer below win.

    ``per_file`` follows that same doctrine one field down: **absent means None, not zero**. A
    ``pacing.json`` written before the field existed would otherwise read as "the operator chose no
    per-image delay" and silently veto the default — see ``merge_pacing``. A *present* but unusable
    value is 0.0 (off) rather than None, because an operator who typed something did express an
    opinion, and it must not invalidate ``min``/``max``, which are the load-bearing half.
    """
    if not isinstance(raw, dict):
        return None
    mode = str(raw.get("mode", "adaptive")).strip().lower()
    if mode not in PACING_MODES:
        return None
    try:
        lo = max(0.0, float(raw["min"]))
        hi = max(0.0, float(raw["max"]))
    except (KeyError, TypeError, ValueError):
        return None
    return {"mode": mode, "min": lo, "max": max(lo, hi), "per_file": _per_file(raw)}


def _per_file(raw: dict[str, Any]) -> float | None:
    """``per_file`` as a clamped float, or None when the block does not mention it at all."""
    if raw.get("per_file") is None:
        return None
    try:
        return min(MAX_PER_FILE_DELAY, max(0.0, float(raw["per_file"])))
    except (TypeError, ValueError):
        return 0.0


def merge_pacing(
    block: dict[str, Any] | None, fallback: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Fill fields the winning layer left unspecified from the layer below it.

    The pacing chain resolves whole blocks — the first layer with a valid one wins outright — which
    is right for ``mode``/``min``/``max``, since they only make sense together. ``per_file`` is
    independent of all three, so a stored block that predates it must not veto the environment's
    value. This is the only place that per-field merge happens.
    """
    if block is None or fallback is None or block.get("per_file") is not None:
        return block
    return {**block, "per_file": fallback.get("per_file")}


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

    # Per-request pacing. Both platforms rate-limit scraping, but they need it in opposite shapes:
    # Instagram gets ~30 posts per JSON request, so a delay amortizes away; Facebook fetches one
    # full HTML page PER PHOTO, so it pays the delay once per image. A fixed delay large enough to
    # be safe on a long Facebook run therefore makes every short one needlessly slow.
    #
    # Hence two modes, and MIN/MAX mean different things in each:
    #   adaptive (default) — MIN is the floor and the starting delay, MAX the back-off ceiling.
    #                        The worker starts at MIN and only slows down when the platform pushes
    #                        back (429 / 403 / block page / login redirect), then decays back down.
    #                        The floor also rises with the number of requests already made, because
    #                        the one block ever observed came ~767 images into a single run.
    #   fixed              — every request sleeps a uniform random value in [MIN, MAX], which is
    #                        gallery-dl's own `sleep-request` behaviour and what this app did
    #                        before adaptive mode.
    # Raise MIN if you get blocked; a block costs far more time than the delay does.
    instagram_pacing_mode: str = "adaptive"
    facebook_pacing_mode: str = "adaptive"
    instagram_sleep_request_min: float = 4.0
    instagram_sleep_request_max: float = 30.0
    facebook_sleep_request_min: float = 1.0
    facebook_sleep_request_max: float = 30.0

    # Per-IMAGE pacing, which is a different axis from everything above and applies in BOTH modes.
    # The settings above space the extractor's API requests; one Instagram request returns ~30
    # posts, and the images it releases then download back to back with nothing pacing them. Real
    # runs measured a median gap of 0.8-2.7 s between consecutive files, and it is that burst, not
    # the run's average, that a rate limiter reacts to. This puts a floor under it.
    #
    # 0 disables it outright — gallery-dl's `build_duration_func` maps 0 to None and skips the call
    # site entirely, so off is byte-for-byte the old behaviour. It costs nothing on skips either:
    # `DownloadJob.handle_url` returns on both the archive and on-disk checks *before* the sleep,
    # so re-running an already-fetched profile is as fast as it ever was.
    #
    # Facebook is deliberately off: `extract_set` fetches a full HTML page per photo, so it already
    # pays the request delay once per image and a second per-file delay would double-charge it.
    instagram_sleep_file: float = 2.0
    facebook_sleep_file: float = 0.0

    def pacing_for(self, platform: str) -> dict[str, Any] | None:
        """The resolved pacing block for a platform, or None if the platform is unknown.

        Shape: ``{"mode", "min", "max", "per_file"}``. Negative values clamp
        to 0 and an inverted range collapses to ``[lo, lo]``, so gallery-dl never sees a range it
        would choke on. ``max <= 0`` in fixed mode means "no pacing at all", expressed as
        ``[0.0, 0.0]`` — which really does disable it, unlike the old sentinel (see the note in
        `_build_payload`).
        """
        pairs = {
            "instagram": (
                self.instagram_pacing_mode,
                self.instagram_sleep_request_min,
                self.instagram_sleep_request_max,
                self.instagram_sleep_file,
            ),
            "facebook": (
                self.facebook_pacing_mode,
                self.facebook_sleep_request_min,
                self.facebook_sleep_request_max,
                self.facebook_sleep_file,
            ),
        }
        entry = pairs.get(platform)
        if entry is None:
            return None
        mode, raw_lo, raw_hi, raw_file = entry
        return normalize_pacing({"mode": mode, "min": raw_lo, "max": raw_hi, "per_file": raw_file})

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
