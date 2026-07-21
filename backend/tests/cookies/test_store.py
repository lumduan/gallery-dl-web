from __future__ import annotations

import os

import pytest

from gallery_dl_web.cookies.store import CookieStore, parse_netscape
from tests.conftest import FB_NETSCAPE


def test_parse_netscape_valid() -> None:
    d = parse_netscape(FB_NETSCAPE)
    assert d["c_user"] == "123456789"
    assert d["xs"] == "12:ABCdef"
    assert d["fr"] == "abcdef"


def test_parse_netscape_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_netscape("just some text\nno tabs here")


def test_roundtrip_and_status(tmp_path) -> None:
    store = CookieStore(tmp_path / "c.json")
    store.update(ig_sessionid="SID123456", fb_cookies_text=FB_NETSCAPE)
    assert store.status() == {"has_ig": True, "has_fb": True}
    assert store.get_for_platform("instagram") == {"sessionid": "SID123456"}
    fb = store.get_for_platform("facebook")
    assert isinstance(fb, dict) and fb["c_user"] == "123456789"

    reloaded = CookieStore(tmp_path / "c.json")
    reloaded.load()
    assert reloaded.has_ig() and reloaded.has_fb()


def test_file_permissions_are_0600(tmp_path) -> None:
    path = tmp_path / "c.json"
    CookieStore(path).update(ig_sessionid="SID")
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_mask() -> None:
    assert CookieStore.mask("abcdef") == "***"
    assert CookieStore.mask("abcdefgh") == "abcd…gh"
    assert CookieStore.mask("") == ""
    assert CookieStore.mask(None) == ""


def test_clear_ig(tmp_path) -> None:
    store = CookieStore(tmp_path / "c.json")
    store.update(ig_sessionid="SID")
    store.update(ig_sessionid="")
    assert store.has_ig() is False


def test_empty_status(tmp_path) -> None:
    assert CookieStore(tmp_path / "c.json").status() == {"has_ig": False, "has_fb": False}


def test_get_for_unknown_platform(tmp_path) -> None:
    assert CookieStore(tmp_path / "c.json").get_for_platform("twitter") is None


def test_load_missing_file_is_empty(tmp_path) -> None:
    store = CookieStore(tmp_path / "missing.json")
    store.load()
    assert store.status() == {"has_ig": False, "has_fb": False}


def test_load_corrupt_file_is_empty(tmp_path) -> None:
    path = tmp_path / "c.json"
    path.write_text("{not json")
    store = CookieStore(path)
    store.load()
    assert store.status() == {"has_ig": False, "has_fb": False}


def test_perms_set_on_save(tmp_path) -> None:
    if os.geteuid() == 0:
        pytest.skip("chmod test not meaningful as root")
    path = tmp_path / "c.json"
    CookieStore(path).update(fb_cookies_text=FB_NETSCAPE)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
