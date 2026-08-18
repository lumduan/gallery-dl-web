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
def _no_real_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any test that actually reaches the transport fails loudly instead of hanging."""
    import requests.adapters

    def _send(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a test tried to make a real HTTP request")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _send)


@pytest.fixture(autouse=True)
def _pristine_extractor() -> Any:
    """gallery-dl's Extractor is class-level state shared by the whole test session.

    ``pacing.install`` patches ``Extractor.request`` / ``_init_session`` and adds a root log
    handler. A test that forgets to uninstall would silently pace, and log, every later test.
    """
    import logging

    from gallery_dl.extractor.common import Extractor

    request, init_session = Extractor.request, Extractor._init_session
    handlers = list(logging.getLogger().handlers)
    timestamp = Extractor.request_timestamp
    yield
    Extractor.request_timestamp = timestamp
    assert Extractor.request is request, "a test left Extractor.request patched"
    assert Extractor._init_session is init_session, "a test left _init_session patched"
    assert logging.getLogger().handlers == handlers, "a test left a root log handler installed"
