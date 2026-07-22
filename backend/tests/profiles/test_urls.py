from __future__ import annotations

from gallery_dl_web.profiles.urls import extract_username, is_safe_profile_name


def test_ig_profile() -> None:
    assert extract_username("https://www.instagram.com/susan/", "instagram") == "susan"
    assert extract_username("https://instagram.com/mike_123", "instagram") == "mike_123"


def test_ig_single_media_excluded() -> None:
    assert extract_username("https://www.instagram.com/p/Cabc123/", "instagram") is None
    assert extract_username("https://www.instagram.com/reel/Cabc/", "instagram") is None
    assert extract_username("https://www.instagram.com/reels/Cabc/", "instagram") is None


def test_ig_stories() -> None:
    assert extract_username("https://www.instagram.com/stories/susan/178/", "instagram") == "susan"


def test_fb_profile() -> None:
    assert extract_username("https://www.facebook.com/susan", "facebook") == "susan"
    assert extract_username("https://facebook.com/mike.kane", "facebook") == "mike.kane"


def test_fb_profile_id() -> None:
    assert (
        extract_username("https://www.facebook.com/profile.php?id=123456", "facebook")
        == "id_123456"
    )


def test_fb_group() -> None:
    assert (
        extract_username("https://www.facebook.com/groups/mygroup/posts/1/", "facebook")
        == "mygroup"
    )


def test_fb_photo_excluded() -> None:
    assert extract_username("https://www.facebook.com/photo/?fbid=1&set=a.2", "facebook") is None


def test_unknown_platform() -> None:
    assert extract_username("https://twitter.com/x", "twitter") is None


def test_safe_name() -> None:
    assert is_safe_profile_name("susan")
    assert is_safe_profile_name("id_123")
    assert is_safe_profile_name("Jane Q Public")  # display names have spaces
    assert is_safe_profile_name("a" * 65)  # long names allowed
    assert not is_safe_profile_name("")
    assert not is_safe_profile_name(".")
    assert not is_safe_profile_name("..")
    assert not is_safe_profile_name("foo/bar")  # path separator
    assert not is_safe_profile_name("../etc")  # contains /
