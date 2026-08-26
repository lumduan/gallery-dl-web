"""The pre-commit hooks and the lockfile must run the same linters.

``.pre-commit-config.yaml`` pins its own ruff and mypy, entirely independently of
``pyproject.toml``/``uv.lock``. Nothing kept them together, and they drifted badly — the hooks were
on ruff ``v0.6.9`` and mypy ``v1.11.2`` while the lock resolved ruff ``0.15.22`` and mypy ``2.3.0``,
a full major apart. ``ruff format`` output differs across that range and mypy 1.x → 2.x is a major
behaviour change, so **a green pre-commit run said nothing about whether CI would pass** — the exact
inverse of what a pre-commit hook is for. CI never ran pre-commit, so nothing surfaced it.

This is a test rather than a CI step so it runs inside the existing ``uv run pytest`` gate, needs no
workflow change, and lives where the repo's other invariants do.

It also makes a *sequencing* rule enforceable: Dependabot raises the hook bump and the lock bump as
separate PRs (they are different ecosystems), and merging either one alone re-opens the gap. With
this test, merging one alone is a build failure rather than a silent regression.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_PRE_COMMIT = _BACKEND / ".pre-commit-config.yaml"
_LOCK = _BACKEND / "uv.lock"

# Hook repo -> the distribution in uv.lock whose version it must match. Data, not conditionals, so
# adding a coupled hook is one line.
COUPLED: dict[str, str] = {
    "https://github.com/astral-sh/ruff-pre-commit": "ruff",
    "https://github.com/pre-commit/mirrors-mypy": "mypy",
}

# `- repo: <url>` followed by `rev: <tag>`. Anchored to consecutive lines so a rev cannot be
# mis-attributed to the wrong repo block.
_REPO_REV_RE = re.compile(r"^\s*-\s*repo:\s*(\S+)\s*\n\s*rev:\s*(\S+)\s*$", re.M)


def _hook_revs() -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _REPO_REV_RE.finditer(_PRE_COMMIT.read_text())}


def _locked_version(package: str) -> str:
    match = re.search(
        rf'^name = "{re.escape(package)}"\nversion = "([^"]+)"$',
        _LOCK.read_text(),
        re.M,
    )
    assert match is not None, f"{package} not found in uv.lock — did the lock format change?"
    return match.group(1)


def test_the_parser_actually_finds_the_hooks() -> None:
    """Guard against the assertion below passing vacuously.

    A regex that matched nothing would make every comparison trivially true — a check whose success
    does not mean what it appears to mean. So the parser is asserted *before* it is trusted.
    """
    revs = _hook_revs()
    missing = set(COUPLED) - set(revs)
    assert not missing, f"parser found no rev for {missing} — fix the parser, not the pins"


@pytest.mark.parametrize(("repo", "package"), sorted(COUPLED.items()))
def test_hook_pin_matches_the_lockfile(repo: str, package: str) -> None:
    """The hook and CI must run the same version of the same linter.

    If this fails after a Dependabot bump, the fix is to land the hook PR and the lock PR together,
    not to relax the check — they are two halves of one change.
    """
    # `removeprefix`, not `lstrip("v")` — lstrip strips a *set of characters* repeatedly, so a rev
    # like "vv1" or a future tag beginning with several v's would be mangled rather than trimmed.
    pinned = _hook_revs()[repo].removeprefix("v")
    locked = _locked_version(package)
    assert pinned == locked, (
        f"{package}: .pre-commit-config.yaml pins {pinned!r} but uv.lock resolves {locked!r}. "
        f"The hooks and CI would run different versions, so a green pre-commit run would not "
        f"predict CI. Merge the matching {repo.rsplit('/', 1)[-1]} and uv.lock bumps together."
    )
