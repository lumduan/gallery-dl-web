"""Worker-side safety nets.

``tests/conftest.py``'s ``_no_real_spawn`` stops the *manager* from launching a worker. These
tests exercise worker code in-process instead, so they need the equivalent guard one layer down:
nothing may reach the network, and nothing may leave a monkeypatch on gallery-dl's classes for
the next test to trip over.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _no_real_http(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any test that actually reaches the transport fails loudly instead of hanging.

    ``@pytest.mark.localhost_http`` opts out, and ``test_capture_e2e.py`` is the only user: the
    claim it exists to verify — that a response body is readable from inside a ``requests`` hook —
    is a property of ``requests`` itself and cannot be shown with the transport stubbed out. It
    binds a loopback server on an ephemeral port, so it still reaches no third party. Opting out by
    marker keeps the guard in force for everything else; deleting it would not.
    """
    if request.node.get_closest_marker("localhost_http"):
        return

    import requests.adapters

    def _send(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a test tried to make a real HTTP request")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _send)


@pytest.fixture(autouse=True)
def _pristine_extractor() -> Any:
    """gallery-dl's Extractor is class-level state shared by the whole test session.

    ``pacing.install`` patches ``Extractor.request`` / ``_init_session`` and adds a root log
    handler; ``upstream_patches.install`` patches ``FacebookExtractor._extract_profile_page``. A
    test that forgets to uninstall would silently pace, log, and re-classify every later test.

    Facebook is imported eagerly rather than probed through ``sys.modules``: the module is small
    and already cached after the first test, and a guard that only checks what happened to be
    imported is exactly the kind of check that reports success because it checked nothing.
    """
    import logging

    from gallery_dl.extractor.common import Extractor
    from gallery_dl.extractor.facebook import FacebookExtractor

    request, init_session = Extractor.request, Extractor._init_session
    profile_page = FacebookExtractor._extract_profile_page
    handlers = list(logging.getLogger().handlers)
    timestamp = Extractor.request_timestamp
    yield
    Extractor.request_timestamp = timestamp
    assert Extractor.request is request, "a test left Extractor.request patched"
    assert Extractor._init_session is init_session, "a test left _init_session patched"
    assert FacebookExtractor._extract_profile_page is profile_page, (
        "a test left FacebookExtractor._extract_profile_page patched"
    )
    assert logging.getLogger().handlers == handlers, "a test left a root log handler installed"
