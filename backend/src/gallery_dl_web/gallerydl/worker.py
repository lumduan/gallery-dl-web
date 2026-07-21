"""gallery-dl worker — a subprocess entrypoint run once per download job.

Why a subprocess: ``gallery_dl.job.DownloadJob`` mutates module-global ``config``/extractor
state, so running it in-process inside the FastAPI server would let concurrent jobs corrupt
each other's cookies and base-directory. A fresh interpreter per job gives complete isolation.

Why STDIN for the payload (which includes cookies): argv leaks to ``ps``/``/proc/<pid>/cmdline``
(world-readable on a multi-user host) and a temp file needs cleanup with unreliable bind-mount
perms. STDIN is one channel that never touches disk or ``ps``.

The worker emits one JSON object per line to stdout (see ``events.emit``). It always emits
exactly one terminal event (``completed`` or ``failed``) last, even on exception.
"""

from __future__ import annotations

import collections
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

HOOK_EVENTS = ("prepare", "file", "skip", "error")


def _now() -> float:
    return time.time()


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default)


def _file_size(path: Any) -> int | None:
    try:
        return os.path.getsize(str(path))
    except OSError:
        return None


def run(
    payload: dict[str, Any],
    *,
    stdout: IO[str] | None = None,
) -> int:
    """Run one job. Returns the worker process exit code (0 = ran gallery-dl, 2 = crash).

    ``stdout``/``stderr`` are injectable for tests; defaults are the real streams.
    """
    job_id = str(payload.get("job_id", "unknown"))
    url = str(payload.get("url", ""))
    preview = bool(payload.get("preview", False))

    # Route gallery-dl's own logging to stderr (never stdout — stdout is our JSON stream).
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    with contextlib.suppress(Exception):  # never let logging setup abort the job
        output.initialize_logging(logging.WARNING)

    # Silence gallery-dl's interactive output entirely; our events are the only stdout content.
    config.set(("output",), "mode", "null")
    config.set(("output",), "progress", False)

    counts = {"downloaded": 0, "skipped": 0, "failed": 0}

    def emit(event: dict[str, Any]) -> None:
        event.setdefault("job_id", job_id)
        event.setdefault("ts", _now())
        events.emit(event)

    def hook_prepare(*args: Any) -> None:
        pf = args[0] if args else None
        emit({"type": "prepare", "filename": _attr(pf, "filename"), "url": url})

    def hook_file(*args: Any) -> None:
        pf = args[0] if args else None
        path = _attr(pf, "path")
        counts["downloaded"] += 1
        emit(
            {
                "type": "file",
                "event": "downloaded",
                "path": str(path) if path is not None else None,
                "filename": _attr(pf, "filename"),
                "bytes": _file_size(path) if path is not None else None,
            }
        )
        emit({"type": "progress", **counts})

    def hook_skip(*args: Any) -> None:
        pf = args[0] if args else None
        path = _attr(pf, "path")
        counts["skipped"] += 1
        emit(
            {
                "type": "file",
                "event": "skipped",
                "path": str(path) if path is not None else None,
                "filename": _attr(pf, "filename"),
            }
        )
        emit({"type": "progress", **counts})

    def hook_error(*args: Any) -> None:
        exc = args[1] if len(args) > 1 else None
        counts["failed"] += 1
        emit(
            {
                "type": "error",
                "message": str(exc) if exc is not None else "download error",
                "kind": type(exc).__name__ if exc is not None else "Error",
                "fatal": False,
            }
        )
        emit({"type": "progress", **counts})

    try:
        config_builder.apply(payload, config)
        emit({"type": "started", "url": url})

        j = job.DataJob(url) if preview else job.DownloadJob(url)
        # gallery-dl leaves Job.hooks as an empty tuple after __init__; it only becomes a
        # defaultdict(list) inside initialize() (during run()), and only when postprocessors are
        # configured. register_hooks indexes hooks by event name, so we must initialize it here.
        # With no postprocessors configured, run()->initialize() does not reset it, so our hooks
        # persist and fire normally.
        j.hooks = collections.defaultdict(list)
        j.register_hooks(
            {"prepare": hook_prepare, "file": hook_file, "skip": hook_skip, "error": hook_error}
        )
        status = j.run()

        terminal_type, reason = events.map_exit_status(status)
        emit(
            {
                "type": terminal_type,
                "exit_status": status,
                "reason": reason,
                **counts,
            }
        )
        return 0
    except BaseException as exc:  # noqa: BLE001 — must always emit a terminal event
        logger.exception("worker crashed for job %s", job_id)
        emit(
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
