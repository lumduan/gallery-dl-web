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
import threading
import time
import types
from typing import IO, Any

# Module-level references so tests can monkeypatch (e.g. ``gallery_dl.job.DownloadJob``).
from gallery_dl import config, job, output

from gallery_dl_web.gallerydl import config_builder, events

logger = logging.getLogger("gallery_dl_web.worker")

# One process = one job, so per-run context lives at module level (reset in run()).
_CTX: dict[str, Any] = {"job_id": "unknown", "url": "", "downloaded": 0, "skipped": 0, "failed": 0}


def _now() -> float:
    return time.time()


def _file_size(path: Any) -> int | None:
    try:
        return os.path.getsize(str(path))
    except OSError:
        return None


def _downloaded_size(kwdict: dict[str, Any]) -> int | None:
    """Size of the file the ``file`` hook just saw.

    gallery-dl fires the ``file`` hook BEFORE ``PathFormat.finalize()`` moves the download into
    place (job.py runs ``hooks["file"]`` and only then calls ``finalize()``, which does
    ``os.replace(temppath, realpath)``). Until then the bytes live at ``temppath`` — ``<path>.part``
    by default — so sizing the *final* path always raised FileNotFoundError and every event
    reported ``bytes: null``.

    ``kwdict["_path"]`` is a ``PathfmtProxy`` forwarding attribute access to the live PathFormat,
    so ask it where the data actually is, newest location first. When part files are disabled
    ``temppath == realpath``, so this stays correct either way.
    """
    proxy = kwdict.get("_path")
    candidates: list[str] = []
    if proxy is not None and not isinstance(proxy, str):
        for attr in ("temppath", "realpath", "path"):
            value = getattr(proxy, attr, None)
            if value:
                candidates.append(str(value))
    resolved = _path_str(kwdict)
    if resolved:
        candidates.append(resolved)
    for candidate in candidates:
        size = _file_size(candidate)
        if size is not None:
            return size
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


def _path_str(kwdict: dict[str, Any]) -> str | None:
    """Resolve the on-disk path from gallery-dl metadata to a plain string.

    ``kwdict['_path']`` may be a string (some extractors) or a gallery-dl PathFormat object
    (e.g. Facebook) which is not JSON-serializable.
    """
    p = kwdict.get("_path")
    if p is None:
        return None
    if isinstance(p, str):
        return p
    return str(getattr(p, "path", p))


def on_prepare(kwdict: dict[str, Any]) -> None:
    """Postprocessor callback: a file's metadata was resolved, about to fetch."""
    _emit({"type": "prepare", "filename": kwdict.get("filename"), "url": _CTX["url"]})


def on_file(kwdict: dict[str, Any]) -> None:
    """Postprocessor callback: a file was downloaded."""
    path = _path_str(kwdict)
    _CTX["downloaded"] += 1
    _emit(
        {
            "type": "file",
            "event": "downloaded",
            "path": path,
            "filename": kwdict.get("filename"),
            "bytes": _downloaded_size(kwdict),
        }
    )
    _emit_progress()


def on_skip(kwdict: dict[str, Any]) -> None:
    """Postprocessor callback: a file was skipped (already in the archive)."""
    path = _path_str(kwdict)
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


# gallery-dl's ``python`` postprocessor resolves ``function`` via util.import_file, which calls
# __import__ — and __import__ returns the TOP-LEVEL package for a dotted name, so a spec like
# "gallery_dl_web.gallerydl.worker:on_file" resolves to the wrong module. Expose the callbacks
# under a single-segment module name in sys.modules so __import__ resolves it directly.
_HOOKS_MODULE = "gallery_dl_web_hooks"
# Any-typed so plain attribute assignment is allowed (mypy skips attr checks on Any; ruff prefers
# assignment over setattr).
_hooks: Any = types.ModuleType(_HOOKS_MODULE)
_hooks.on_prepare = on_prepare
_hooks.on_file = on_file
_hooks.on_skip = on_skip
sys.modules[_HOOKS_MODULE] = _hooks

# One PP per event; each calls the named function with pathfmt.kwdict (which has _path + filename).
# Extractor-level config => gallery-dl applies these to EVERY job, including child jobs.
_POSTPROCESSORS = [
    {"name": "python", "function": f"{_HOOKS_MODULE}:on_prepare", "event": "prepare"},
    {"name": "python", "function": f"{_HOOKS_MODULE}:on_file", "event": "file"},
    {"name": "python", "function": f"{_HOOKS_MODULE}:on_skip", "event": "skip"},
]


def _start_heartbeat(interval: float) -> threading.Event:
    """Emit a ``heartbeat`` event every ``interval`` seconds until the returned Event is set.

    gallery-dl is completely silent while it enumerates a profile — that can legitimately take
    minutes (Instagram sleeps 6-12s between requests). Without this the manager cannot tell
    "working, just slow" from "wedged", and the UI looks frozen. Daemon thread so a crash in the
    main thread can never leave the process hanging on it.
    """
    stop = threading.Event()

    def _beat() -> None:
        beats = 0
        while not stop.wait(interval):
            beats += 1
            with contextlib.suppress(Exception):  # a broken stdout must not kill the job
                _emit({"type": "heartbeat", "beat": beats, "elapsed": beats * interval})

    threading.Thread(target=_beat, name="worker-heartbeat", daemon=True).start()
    return stop


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

    heartbeat: threading.Event | None = None
    try:
        config_builder.apply(payload, config)
        _emit({"type": "started", "url": url})

        interval = float(payload.get("heartbeat_seconds", 15.0))
        if interval > 0:
            heartbeat = _start_heartbeat(interval)

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
    finally:
        # A late beat may still slip out after the terminal event; harmless — the manager stops
        # reading at the terminal event and reaps the process.
        if heartbeat is not None:
            heartbeat.set()


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
