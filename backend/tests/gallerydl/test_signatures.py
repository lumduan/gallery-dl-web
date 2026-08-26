"""The pushback signature table, checked against what a real block actually looked like.

The fixtures in the first class are **transcribed from a captured run** (2026-08-26): an Instagram
job at ``adaptive 4-30`` that died after 859 downloads and 885 s. Every value here — statuses,
paths, content types — is what Instagram sent, with the profile's numeric id replaced by a
placeholder because the repo is public and it identifies a third party.

That capture is the reason this module exists. The original hypothesis was an HTTP 200 carrying
``{"status": "fail"}``; the reality was a 302 to the bare domain root, which the old classifier
scored ``clean`` along with all 49 other requests in the ring buffer.
"""

from __future__ import annotations

import pytest

from gallery_dl_web.gallerydl.signatures import Observation, Tier, classify

# Stands in for the real numeric profile id in the captured URLs.
PROFILE_ID = "11111111111"


def obs(
    url: str,
    status: int = 200,
    content_type: str = "application/json; charset=utf-8",
    body: str = "",
    streamed: bool = False,
) -> Observation:
    return Observation(
        status=status, url=url, content_type=content_type, body=body, streamed=streamed
    )


# --- what the real block looked like --------------------------------------------------------------


class TestTheCapturedBlock:
    """Transcribed from the ring buffer of the run that died at t+885s."""

    def test_the_302_that_started_it(self) -> None:
        """i=937: the API request that got bounced. Empty body, html content type."""
        tier, rule = classify(
            "instagram",
            obs(
                "https://www.instagram.com/api/v1/clips/user/",
                status=302,
                content_type="text/html; charset=utf-8",
            ),
        )
        assert tier is Tier.THROTTLE
        assert rule == "ig-html-for-api"

    def test_the_landing_on_the_home_page(self) -> None:
        """i=938: where it landed. THE signature — this is what a block is."""
        tier, rule = classify(
            "instagram",
            obs(
                "https://www.instagram.com/",
                content_type='text/html; charset="utf-8"',
                body='<!DOCTYPE html><html class="_9dls _ar44" lang="en" dir="ltr"><head>',
            ),
        )
        assert tier is Tier.TERMINAL
        assert rule == "ig-redirect-root"

    def test_the_healthy_requests_around_it_stay_clean(self) -> None:
        """i=934 and i=939: real feed/info responses from the same buffer, seconds earlier.

        A classifier that flags these is worse than useless — it would back off constantly.
        """
        for url in (
            f"https://www.instagram.com/api/v1/feed/user/{PROFILE_ID}/",
            f"https://www.instagram.com/api/v1/users/{PROFILE_ID}/info/",
        ):
            tier, rule = classify("instagram", obs(url, body='{"items":[{"pk":"1"}]}'))
            assert tier is Tier.CLEAN, f"{url} must not trip a signature"
            assert rule is None

    def test_media_downloads_stay_clean(self) -> None:
        """43 of the 50 buffered requests were CDN media. All clean, none body-read."""
        tier, rule = classify(
            "instagram",
            obs(
                "https://instagram.fbkk13-3.fna.fbcdn.net/v/t51.82787-15/58901234_n.webp",
                content_type="image/webp",
                streamed=True,
            ),
        )
        assert tier is Tier.CLEAN
        assert rule is None

    def test_the_old_classifier_would_have_called_the_block_clean(self) -> None:
        """The regression, stated as a test: status + the old URL markers see nothing here.

        302 is in no penalty set, and `instagram.com/` contains neither `/accounts/login` nor any
        Facebook marker — so the pre-signature `_classify` returned None, i.e. CLEAN, and fed the
        streak. That is why all 50 entries in the captured buffer read `clean`.
        """
        block = obs("https://www.instagram.com/", content_type="text/html")
        assert block.status not in (429, 403, 503)
        assert "/accounts/login" not in block.path
        # ...yet the table catches it.
        assert classify("instagram", block)[0] is Tier.TERMINAL


# --- the tiers ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected_tier", "expected_rule"),
    [
        (429, Tier.THROTTLE, "http-429"),
        (403, Tier.THROTTLE, "http-403"),
        (503, Tier.THROTTLE, "http-503"),
        (900, Tier.THROTTLE, "http-900"),
        (404, Tier.CLEAN, None),  # a deleted post says nothing about our request rate
        (200, Tier.CLEAN, None),
    ],
)
def test_status_tiers(status: int, expected_tier: Tier, expected_rule: str | None) -> None:
    assert classify("instagram", obs("https://x.instagram.com/api/v1/x", status=status)) == (
        expected_tier,
        expected_rule,
    )


@pytest.mark.parametrize(
    ("body", "expected_rule"),
    [
        ('{"message":"checkpoint_required","status":"fail"}', "ig-account-flagged"),
        ('{"message":"challenge_required"}', "ig-account-flagged"),
        ('{"message":"feedback_required","spam":true}', "ig-account-flagged"),
    ],
)
def test_account_flagged_bodies_are_terminal(body: str, expected_rule: str) -> None:
    """These need TERMINAL, not back-off: retrying into a checkpoint extends the block."""
    tier, rule = classify("instagram", obs("https://www.instagram.com/api/v1/x", body=body))
    assert tier is Tier.TERMINAL
    assert rule == expected_rule


def test_the_wait_message_is_a_throttle_not_a_stop() -> None:
    """Transient by definition — the platform is telling us when to come back."""
    tier, rule = classify(
        "instagram",
        obs(
            "https://www.instagram.com/api/v1/x",
            body='{"status":"fail","message":"Please wait a few minutes before you try again."}',
        ),
    )
    assert tier is Tier.THROTTLE
    assert rule == "ig-wait-message"


def test_unparseable_json_is_unknown_not_clean() -> None:
    """THE tier that used to be missing.

    "We could not tell" is not evidence of health. Scoring it clean is half of why the old
    controller could not react: every unrecognised response advanced the streak.
    """
    tier, rule = classify(
        "instagram",
        obs("https://www.instagram.com/api/v1/x", body="<html>nope</html>"),
    )
    assert tier is Tier.UNKNOWN
    assert rule == "unparseable-json"


# --- Facebook, which must keep working ------------------------------------------------------------


def test_facebook_block_page_still_matches_gallery_dls_own_marker() -> None:
    tier, rule = classify(
        "facebook",
        obs(
            "https://www.facebook.com/photo/",
            content_type="text/html",
            body='x{"__dr":"CometErrorRoot.react"}y',
        ),
    )
    assert tier is Tier.TERMINAL
    assert rule == "fb-block-page"


def test_facebook_login_redirect_is_terminal() -> None:
    tier, rule = classify("facebook", obs("https://www.facebook.com/login/?next=x"))
    assert tier is Tier.TERMINAL
    assert rule == "fb-redirect-login"


# --- table hygiene --------------------------------------------------------------------------------


def test_an_unknown_platform_runs_every_table_rather_than_none() -> None:
    """Failing toward detection. Safe because the rules are host- and marker-scoped."""
    assert classify("", obs("https://www.instagram.com/"))[1] == "ig-redirect-root"
    assert classify("tiktok", obs("https://www.facebook.com/login/"))[1] == "fb-redirect-login"


def test_a_raising_signature_cannot_break_a_download() -> None:
    """A bad rule degrades to 'no match', never to a failed job."""
    from gallery_dl_web.gallerydl import signatures

    boom = signatures.Signature("boom", Tier.TERMINAL, lambda o: 1 / 0 > 0)  # type: ignore[operator]
    original = signatures.PLATFORMS["instagram"]
    signatures.PLATFORMS["instagram"] = (boom, *original)
    try:
        assert classify("instagram", obs("https://www.instagram.com/api/v1/x"))[0] is Tier.CLEAN
    finally:
        signatures.PLATFORMS["instagram"] = original


def test_every_rule_name_is_unique_and_platform_prefixed() -> None:
    """The name reaches the operator and the SSE stream, so it has to identify itself."""
    from gallery_dl_web.gallerydl import signatures

    # Explicit, not derived from the platform name: `instagram[:2]` is "in", and an assertion
    # that only passes because of a coincidence between two unrelated strings is not a guard.
    prefixes = {"instagram": "ig-", "facebook": "fb-"}
    assert set(prefixes) == set(signatures.PLATFORMS), "a platform has no declared rule prefix"
    for platform, table in signatures.PLATFORMS.items():
        for sig in table:
            assert sig.name.startswith(prefixes[platform]), (
                f"{sig.name} is not prefixed for {platform}"
            )
    names = [s.name for t in signatures.PLATFORMS.values() for s in t]
    assert len(names) == len(set(names)), "duplicate rule names"
