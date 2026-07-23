"""Per-request pacing (gallery-dl sleep-request) is env-tunable and reaches the worker payload."""

from __future__ import annotations

from gallery_dl_web.config import Settings


def test_sleep_request_for_known_platforms(tmp_settings: Settings) -> None:
    assert tmp_settings.sleep_request_for("instagram") == [6.0, 12.0]
    assert tmp_settings.sleep_request_for("facebook") == [3.0, 8.0]
    assert tmp_settings.sleep_request_for("tiktok") is None


def test_sleep_request_can_be_disabled_and_is_sane(tmp_settings: Settings) -> None:
    tmp_settings.facebook_sleep_request_max = 0  # explicit opt-out
    assert tmp_settings.sleep_request_for("facebook") is None

    # A max below min must not produce an inverted range gallery-dl would choke on.
    tmp_settings.facebook_sleep_request_min = 9
    tmp_settings.facebook_sleep_request_max = 4
    assert tmp_settings.sleep_request_for("facebook") == [9.0, 9.0]


def test_payload_carries_pacing(job_manager, tmp_settings: Settings) -> None:
    from gallery_dl_web.jobs.models import JobState

    tmp_settings.facebook_sleep_request_min = 5
    tmp_settings.facebook_sleep_request_max = 11
    st = JobState(id="j", url="https://facebook.com/someone", platform="facebook")
    payload = job_manager._build_payload(st, {}, {"c_user": "1"})
    assert payload["options"]["sleep-request"] == [5.0, 11.0]


def test_explicit_job_option_beats_settings(job_manager, tmp_settings: Settings) -> None:
    from gallery_dl_web.jobs.models import JobState

    tmp_settings.facebook_sleep_request_min = 5
    tmp_settings.facebook_sleep_request_max = 11
    st = JobState(id="j", url="https://facebook.com/someone", platform="facebook")
    payload = job_manager._build_payload(st, {"sleep-request": [1.0, 2.0]}, {"c_user": "1"})
    assert payload["options"]["sleep-request"] == [1.0, 2.0]
