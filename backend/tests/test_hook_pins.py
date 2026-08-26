"""The pre-commit hooks and CI must run the same linters, the same way.

``.pre-commit-config.yaml`` used to pin its own ruff and mypy independently of ``uv.lock``, and
nothing kept them together — the hooks sat on ruff ``v0.6.9`` and mypy ``v1.11.2`` while the lock
resolved ``0.15.22`` and ``2.3.0``, a full major apart. **A green pre-commit run said nothing about
whether CI would pass**, the inverse of what a pre-commit hook is for, and CI never runs pre-commit
so nothing surfaced it.

There are two ways to close that, and this repo now uses both:

* **ruff** stays a pinned mirror, and ``test_hook_pin_matches_the_lockfile`` asserts the pin equals
  the locked version. That also makes a *sequencing* rule enforceable: Dependabot raises the hook
  bump and the lock bump as separate PRs because they are separate ecosystems, and merging either
  alone now fails the build instead of silently reopening the gap.
* **mypy** is a ``repo: local`` hook invoking the project's own environment. There is no version to
  keep in step, so it cannot drift *by construction* — strictly stronger than a comparison, which
  is why it is absent from ``COUPLED``.

The third test here is the one that would have caught the original bug on day one: a ``files:``
pattern that matches nothing makes a hook report "Skipped", which reads as success. The old mypy
hook was configured that way from the day it was written and had never checked a single file.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND.parent
_PRE_COMMIT = _BACKEND / ".pre-commit-config.yaml"
_LOCK = _BACKEND / "uv.lock"
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Mirrored hook repo -> the distribution in uv.lock whose version it must match. Data, not
# conditionals, so adding a coupled hook is one line. mypy is deliberately NOT here: it is a local
# hook with no version of its own (see test_the_mypy_hook_runs_the_projects_own_environment).
COUPLED: dict[str, str] = {
    "https://github.com/astral-sh/ruff-pre-commit": "ruff",
}

# The command CI runs, and which the local mypy hook must reproduce exactly.
MYPY_COMMAND = "mypy src"

# `- repo: <url>` followed by `rev: <tag>`. Anchored to consecutive lines so a rev cannot be
# mis-attributed to the wrong repo block.
_REPO_REV_RE = re.compile(r"^\s*-\s*repo:\s*(\S+)\s*\n\s*rev:\s*(\S+)\s*$", re.M)


def _hook_revs() -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _REPO_REV_RE.finditer(_PRE_COMMIT.read_text())}


def _hook_file_patterns() -> dict[str, str]:
    """Hook id -> its ``files:`` pattern, for every hook that declares one."""
    patterns: dict[str, str] = {}
    current: str | None = None
    for line in _PRE_COMMIT.read_text().splitlines():
        if match := re.match(r"\s*-\s*id:\s*(\S+)", line):
            current = match.group(1)
        elif current and (match := re.match(r"\s*files:\s*(\S+)", line)):
            patterns[current] = match.group(1)
    return patterns


def _locked_version(package: str) -> str:
    match = re.search(
        rf'^name = "{re.escape(package)}"\nversion = "([^"]+)"$',
        _LOCK.read_text(),
        re.M,
    )
    assert match is not None, f"{package} not found in uv.lock — did the lock format change?"
    return match.group(1)


def _tracked_files() -> list[str]:
    """Paths as pre-commit sees them: relative to the git root."""
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - git is always present
        pytest.skip(f"git unavailable: {exc}")
    return result.stdout.splitlines()


# --- the mirrored hooks, kept in step with the lockfile -------------------------------------------


def test_the_parser_actually_finds_the_hooks() -> None:
    """Guard against the comparison below passing vacuously.

    A regex that matched nothing would make every assertion trivially true — a check whose success
    does not mean what it appears to mean. So the parser is asserted before it is trusted.
    """
    missing = set(COUPLED) - set(_hook_revs())
    assert not missing, f"parser found no rev for {missing} — fix the parser, not the pins"


@pytest.mark.parametrize(("repo", "package"), sorted(COUPLED.items()))
def test_hook_pin_matches_the_lockfile(repo: str, package: str) -> None:
    """The hook and CI must run the same version of the same linter.

    If this fails after a Dependabot bump, the fix is to land the hook PR and the lock PR together,
    not to relax the check — they are two halves of one change.
    """
    # `removeprefix`, not `lstrip("v")` — lstrip strips a *set of characters* repeatedly, so a rev
    # beginning with several v's would be mangled rather than trimmed.
    pinned = _hook_revs()[repo].removeprefix("v")
    locked = _locked_version(package)
    assert pinned == locked, (
        f"{package}: .pre-commit-config.yaml pins {pinned!r} but uv.lock resolves {locked!r}. "
        f"The hooks and CI would run different versions, so a green pre-commit run would not "
        f"predict CI. Merge the matching {repo.rsplit('/', 1)[-1]} and uv.lock bumps together."
    )


# --- the local hook, which cannot drift at all ----------------------------------------------------


def test_the_mypy_hook_runs_the_projects_own_environment() -> None:
    """A mirrored mypy cannot see fastapi, so `--strict` drowns in `untyped-decorator`.

    Pinning it and listing every runtime dependency under `additional_dependencies` would fix that
    by duplicating uv.lock — a second source of truth, which is the problem this file exists to
    prevent. A local hook has no version at all.
    """
    config = _PRE_COMMIT.read_text()
    assert "mirrors-mypy" not in config, (
        "mypy went back to a pinned mirror. An isolated mypy cannot see the project's "
        "dependencies; if that is deliberate it needs additional_dependencies and a COUPLED entry."
    )
    assert "repo: local" in config
    assert "pass_filenames: false" in config, "mypy needs the whole package for inference"


def test_the_mypy_hook_runs_the_same_command_as_ci() -> None:
    """The point of the local hook: one command, one environment, no version to keep in step."""
    entry = re.search(r"^\s*entry:\s*(.+)$", _PRE_COMMIT.read_text(), re.M)
    assert entry is not None, "the local mypy hook has no entry"
    assert entry.group(1).strip().endswith(MYPY_COMMAND), (
        f"the hook must end with {MYPY_COMMAND!r}, the command ci.yml runs"
    )
    assert f"uv run {MYPY_COMMAND}" in _CI.read_text(), (
        f"ci.yml no longer runs {MYPY_COMMAND!r} — update MYPY_COMMAND and the hook together"
    )


# --- the bug that started all of this -------------------------------------------------------------


def test_every_files_pattern_matches_at_least_one_tracked_file() -> None:
    """A `files:` pattern matching nothing makes a hook report "Skipped", which reads as success.

    The old mypy hook used `files: ^src/`, but pre-commit passes paths relative to the GIT ROOT, so
    they are `backend/src/...` — 36 files match `^backend/src/` and 0 match `^src/`. It had never
    checked a single file, from the day it was written, and looked green throughout.
    """
    patterns = _hook_file_patterns()
    assert patterns, "no hook declares a `files:` pattern — did the parser break?"
    tracked = _tracked_files()

    for hook_id, pattern in sorted(patterns.items()):
        # pre-commit filters with `re.search`, not `re.match`.
        matched = [path for path in tracked if re.search(pattern, path)]
        assert matched, (
            f"hook {hook_id!r} has files={pattern!r}, which matches none of the "
            f"{len(tracked)} tracked files. It will silently report 'Skipped' on every run. "
            f"Remember pre-commit paths are relative to the git root (e.g. 'backend/src/...')."
        )
