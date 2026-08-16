"""Rate-limit and login-wall classification, driven by real gallery-dl stderr output."""

from __future__ import annotations

from gallery_dl_web.gallerydl.errors import detect_login_wall, detect_rate_limit

# The shape of a real gallery-dl run (a Facebook profile blocked after 104 images), with every
# identifier replaced by a placeholder. This repo is public: never commit a real username, profile
# id or media id lifted from live output.
FB_BLOCK = [
    '….facebook.com:443 "GET /photo/?fbid=1&set=a.2 HTTP/1.1" 200 None',
    "debug:facebook:",
    "Traceback (most recent call last):",
    '  File "/opt/venv/lib/python3.12/site-packages/gallery_dl/job.py", line 163, in run',
    "    msg = self.dispatch(extractor)",
    "gallery_dl.exception.AbortExtraction: You've been temporarily blocked from viewing images.",
    "Please try using a different account, using a VPN or waiting before you retry.",
    'You can use this URL to continue from where you left off (added "&setextract"):',
    "https://www.facebook.com/photo/?fbid=1&set=a.2&setextract",
    "info:facebook:No results for https://www.facebook.com/example.invalid/avatar",
]


def test_detects_facebook_block_and_resume_url() -> None:
    limit = detect_rate_limit(FB_BLOCK)
    assert limit is not None
    assert "temporarily blocked" in limit.message.lower()
    # Plain language for the operator, not a traceback.
    assert "Traceback" not in limit.message
    assert limit.resume_url == ("https://www.facebook.com/photo/?fbid=1&set=a.2&setextract")


def test_http_200_does_not_fool_it() -> None:
    """Facebook serves the block page with HTTP 200, so status codes prove nothing."""
    assert "200 None" in FB_BLOCK[0]
    assert detect_rate_limit(FB_BLOCK) is not None


def test_detects_instagram_wording() -> None:
    limit = detect_rate_limit(["error:instagram:Please wait a few minutes before you try again."])
    assert limit is not None
    assert "Instagram" in limit.message
    assert limit.resume_url is None


def test_detects_429_and_challenge() -> None:
    for line in (
        "urllib3: 429 Too Many Requests",
        "HTTP 429",
        "429 Client Error: Too Many Requests for url: ...",
        "error: challenge_required",
    ):
        assert detect_rate_limit([line]) is not None, line


def test_ordinary_failures_are_not_rate_limits() -> None:
    """A normal download error must keep its own reason and raw tail."""
    for line in (
        "error:facebook:HttpError: '404 Not Found'",
        "PermissionError: [Errno 13] Permission denied: '/mnt/downloads/gallery'",
        "warning:instagram:Unable to fetch data for 12345",
        # A bare 429 in unrelated text must not trigger it — file counts, ids, filenames.
        "downloading 429 files",
        "saved 2019-04-25_429_Bwq9aJOgETf.jpg",
        "fbid=4290000000000429",
    ):
        assert detect_rate_limit([line]) is None, line


def test_empty_input() -> None:
    assert detect_rate_limit([]) is None


# --------------------------------------------------------------------------- login walls

# gallery-dl's own AuthRequired wording. `exception.py` composes "<auth> needed to access this
# <resource>"; the Facebook extractor raises the other two directly. Placeholders only — this file
# once shipped a real Facebook username and that person's photo/album ids for several releases.
LOGIN_WALL_LINES = [
    "gallery_dl.exception.AuthRequired: You must be logged in to continue viewing images.",
    "gallery_dl.exception.AuthRequired: authenticated cookies needed to access this profile "
    "('This content isn't available right now')",
    "gallery_dl.exception.AuthRequired: Account credentials required",
    "error:instagram:authenticated cookies needed to access this resource",
    # Observed live: an anonymous IG job gets no AuthRequired at all, just this.
    "gallery_dl.exception.HttpError: '401 Unauthorized' for "
    "'https://www.instagram.com/graphql/query/?query_hash=0&variables=%7B%7D'",
    'error:instagram:{"message": "login_required", "status": "fail"}',
]


def test_detects_each_login_wall_wording() -> None:
    for line in LOGIN_WALL_LINES:
        assert detect_login_wall([line]) is True, line


def test_login_wall_is_case_insensitive() -> None:
    assert detect_login_wall(["YOU MUST BE LOGGED IN to continue viewing images."]) is True


def test_ordinary_failures_are_not_login_walls() -> None:
    """Narrow on purpose: this reason sends the operator to Settings for cookies."""
    for line in (
        "error:facebook:HttpError: '404 Not Found'",
        "PermissionError: [Errno 13] Permission denied: '/mnt/downloads/gallery'",
        "info:instagram:logged in as someone",
        "warning:instagram:Unable to fetch data for 12345",
        # A bare 401 in unrelated text must not trigger it — ids, counts, filenames.
        "downloading 401 files",
        "saved 2019-04-25_401_Bwq9aJOgETf.jpg",
        "fbid=4010000000000401",
        # 403 is deliberately excluded: platforms serve it for blocks, where "wait" is the advice.
        "gallery_dl.exception.HttpError: '403 Forbidden'",
    ):
        assert detect_login_wall([line]) is False, line


def test_login_wall_empty_input() -> None:
    assert detect_login_wall([]) is False


def test_facebook_block_page_is_a_rate_limit_not_a_login_wall() -> None:
    """Both classifiers can match FB's block output; the manager must prefer 'wait it out'.

    Telling the operator to add cookies for a block that cookies cannot fix would send them off to
    re-export a session that is already fine, and a retry extends the block.
    """
    assert detect_rate_limit(FB_BLOCK) is not None
