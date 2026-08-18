"""Per-request pacing: how the three layers resolve, and what reaches the worker payload."""

from __future__ import annotations

from typing import Any

from gallery_dl_web.config import Settings
from gallery_dl_web.jobs.models import JobState


def _state() -> JobState:
    return JobState(id="j", url="https://facebook.com/someone", platform="facebook")


def test_env_defaults_are_adaptive_and_facebook_is_the_fast_one(tmp_settings: Settings) -> None:
    """Facebook fetches a page per photo where Instagram gets ~30 posts per request, so the same
    delay costs Facebook ~30x more. Its floor is correspondingly lower."""
    assert tmp_settings.pacing_for("facebook") == {"mode": "adaptive", "min": 1.0, "max": 30.0}
    assert tmp_settings.pacing_for("instagram") == {"mode": "adaptive", "min": 4.0, "max": 30.0}
    assert tmp_settings.pacing_for("tiktok") is None


def test_fixed_mode_is_still_available(tmp_settings: Settings) -> None:
    tmp_settings.facebook_pacing_mode = "fixed"
    tmp_settings.facebook_sleep_request_min = 3
    tmp_settings.facebook_sleep_request_max = 8
    assert tmp_settings.pacing_for("facebook") == {"mode": "fixed", "min": 3.0, "max": 8.0}


def test_bad_values_are_repaired_not_propagated(tmp_settings: Settings) -> None:
    # An inverted range would make gallery-dl's random.uniform raise.
    tmp_settings.facebook_sleep_request_min = 9
    tmp_settings.facebook_sleep_request_max = 4
    assert tmp_settings.pacing_for("facebook") == {"mode": "adaptive", "min": 9.0, "max": 9.0}
    tmp_settings.facebook_sleep_request_min = -1
    tmp_settings.facebook_sleep_request_max = -1
    assert tmp_settings.pacing_for("facebook") == {"mode": "adaptive", "min": 0.0, "max": 0.0}
    # An unrecognised mode means "this layer has no opinion", not a broken job.
    tmp_settings.facebook_pacing_mode = "turbo"
    assert tmp_settings.pacing_for("facebook") is None


def test_payload_carries_pacing_at_the_top_level(job_manager: Any, tmp_settings: Settings) -> None:
    """Beside `anonymous`, not inside `options` — config_builder only reads keys it knows."""
    tmp_settings.facebook_sleep_request_min = 2
    tmp_settings.facebook_sleep_request_max = 20
    payload = job_manager._build_payload(_state(), {}, {"c_user": "1"}, False)
    assert payload["pacing"] == {"mode": "adaptive", "min": 2.0, "max": 20.0}
    assert "pacing" not in payload["options"]


def test_precedence_is_job_then_store_then_env(job_manager: Any, tmp_settings: Settings) -> None:
    store = job_manager._pacing_store
    env = job_manager._build_payload(_state(), {}, None, True)["pacing"]
    assert env["min"] == 1.0

    store.update("facebook", {"mode": "fixed", "min": 3.0, "max": 8.0})
    assert job_manager._build_payload(_state(), {}, None, True)["pacing"]["mode"] == "fixed"

    job = {"mode": "adaptive", "min": 0.5, "max": 5.0}
    assert job_manager._build_payload(_state(), {}, None, True, job)["pacing"]["min"] == 0.5


def test_a_malformed_job_override_falls_through(job_manager: Any) -> None:
    """A bad per-job value must not break the job — the layer below still has a valid answer."""
    payload = job_manager._build_payload(_state(), {}, None, True, {"mode": "warp", "min": "x"})
    assert payload["pacing"]["mode"] == "adaptive"


def test_the_ceiling_is_clamped_against_the_stall_detector(
    job_manager: Any, tmp_settings: Settings
) -> None:
    """A job legitimately backed off past the progress deadline would be killed as stalled while
    behaving exactly as designed — and a Facebook retry re-walks the linked list from the top."""
    tmp_settings.stall_cap_seconds = 600.0
    tmp_settings.facebook_sleep_request_max = 500
    assert job_manager._build_payload(_state(), {}, None, True)["pacing"]["max"] == 150.0


def test_the_clamp_never_inverts_the_range(job_manager: Any, tmp_settings: Settings) -> None:
    tmp_settings.stall_cap_seconds = 20.0  # clamps to 5.0, below the floor
    tmp_settings.facebook_sleep_request_min = 30
    tmp_settings.facebook_sleep_request_max = 40
    pacing = job_manager._build_payload(_state(), {}, None, True)["pacing"]
    assert pacing["min"] == 30.0
    assert pacing["max"] == 30.0
