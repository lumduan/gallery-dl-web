"""gallery-dl worker — a subprocess entrypoint run once per download job.

Why a subprocess: ``gallery_dl.job.DownloadJob`` mutates module-global ``config``/extractor
state, so running it in-process inside the FastAPI server would let concurrent jobs corrupt
each other's cookies and base-directory. A fresh interpreter per job gives complete isolation.

Why STDIN for the payload (which includes cookies): argv leaks to ``ps``/``/proc/<pid>/cmdline``
(world-readable on a multi-user host) and a temp file needs cleanup with unreliable bind-mount
perms. STDIN is one channel that never touches disk or ``ps``.

Per-file progress: we register our callbacks as a gallery-dl **postprocessor** (the ``python``
postprocessor), NOT via ``Job.register_hooks``. Postprocessors are extractor-level config, so
gallery-dl applies them to **every** job — including the child jobs that e.g. Facebook profile
extraction spawns to walk photos/albums. ``register_hooks`` on a single ``DownloadJob`` only covers
that one job and silently misses child downloads.

The worker emits one JSON object per line to stdout (see ``events.emit``). It always emits exactly
one terminal event (``completed`` or ``failed``) last, even on exception.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
from typing import IO, Any

# Module-level references so tests can monkeypatch (e.g. ``gallery_dl.job.DownloadJob``).
from gallery_dl import config, job, output

from gallery_dl_web.gallerydl import config_builder, events

logger = logging.getLogger("gallery_dl_web.worker")

_MOD = "gallery_dl_web.gallerydl.worker"

# One process = one job, so per-run context lives at module level (reset in run()).
_CTX: dict[str, Any] = {"job_id": "unknown", "url": "", "downloaded": 0, "skipped": 0, "failed": 0}

# gallery-dl ``python`` postprocessor: calls ``function(kwdict)`` on each event. We register one
# per event we care about (prepare / file / skip). The function receives ``pathfmt.kwdict``, which
# contains ``_path`` (full on-disk path) and ``filename``. Applied to all jobs incl. children.
_POSTPROCESSORS = [
    {"name": "python", "function": f"{_MOD}:on_prepare", "event": "prepare"},
    {"name": "python", "function": f"{_MOD}:on_file", "event": "file"},
    {"name": "python", "function": f"{_MOD}:on_skip", "event": "skip"},
]


def _now() -> float:
    return time.time()


def _file_size(path: Any) -> int | None:
    try:
        return os.path.getsize(str(path))
    except OSError:
        return None


def _emit(event: dict[str, Any]) -> None:
    event.setdefault("job_id", _CTX["job_id"])
    event.setdefault("ts", _now())
    events.emit(event)


def _emit_progress() -> None:
    _emit(
        {
            "type": "progress",
            "downloaded": _CTX["downloaded"],
            "skipped": _CTX["skipped"],
            "failed": _CTX["failed"],
        }
    )


def on_prepare(kwdict: dict[str, Any]) -> None:
    """Postprocessor callback: a file's metadata was resolved, about to fetch."""
    _emit({"type": "prepare", "filename": kwdict.get("filename"), "url": _CTX["url"]})


def on_file(kwdict: dict[str, Any]) -> None:
    """Postprocessor callback: a file was downloaded."""
    path = kwdict.get("_path")
    _CTX["downloaded"] += 1
    _emit(
        {
            "type": "file",
            "event": "downloaded",
            "path": path,
            "filename": kwdict.get("filename"),
            "bytes": _file_size(path) if path else None,
        }
    )
    _emit_progress()


def on_skip(kwdict: dict[str, Any]) -> None:
    """Postprocessor callback: a file was skipped (already in the archive)."""
    path = kwdict.get("_path")
    _CTX["skipped"] += 1
    _emit(
        {
            "type": "file",
            "event": "skipped",
            "path": path,
            "filename": kwdict.get("filename"),
        }
    )
    _emit_progress()


def run(payload: dict[str, Any]) -> int:
    """Run one job. Returns the worker process exit code (0 = ran gallery-dl, 2 = crash)."""
    job_id = str(payload.get("job_id", "unknown"))
    url = str(payload.get("url", ""))
    preview = bool(payload.get("preview", False))
    _CTX.update(job_id=job_id, url=url, downloaded=0, skipped=0, failed=0)

    # Route gallery-dl's own logging to stderr (never stdout — stdout is our JSON stream).
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    with contextlib.suppress(Exception):  # never let logging setup abort the job
        output.initialize_logging(logging.WARNING)

    # Silence gallery-dl's interactive output entirely; our events are the only stdout content.
    config.set(("output",), "mode", "null")
    config.set(("output",), "progress", False)
    # Register our progress callbacks as a postprocessor (applies to parent + child jobs).
    config.set(("extractor",), "postprocessors", _POSTPROCESSORS)

    try:
        config_builder.apply(payload, config)
        _emit({"type": "started", "url": url})

        j = job.DataJob(url) if preview else job.DownloadJob(url)
        status = j.run()

        terminal_type, reason = events.map_exit_status(status)
        _emit(
            {
                "type": terminal_type,
                "exit_status": status,
                "reason": reason,
                "downloaded": _CTX["downloaded"],
                "skipped": _CTX["skipped"],
                "failed": _CTX["failed"],
            }
        )
        return 0
    except BaseException as exc:  # noqa: BLE001 — must always emit a terminal event
        logger.exception("worker crashed for job %s", job_id)
        _emit(
            {
                "type": "failed",
                "exit_status": 2,
                "reason": "worker-crash",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )
        return 2


def main(stdin: IO[str] | None = None) -> int:
    """CLI entry: read one JSON payload from stdin, run the job."""
    stream = stdin or sys.stdin
    try:
        raw = stream.read()
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        # Cannot emit a job-scoped event (no job_id known) — write a crash line and exit.
        events.emit(
            {
                "type": "failed",
                "job_id": "unknown",
                "exit_status": 2,
                "reason": "bad-stdin",
                "message": f"could not parse stdin payload: {exc}",
                "ts": _now(),
            }
        )
        return 2
    return run(payload)


if __name__ == "__main__":
    sys.exit(main())
