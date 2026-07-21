"""Spawn the gallery-dl worker as an isolated subprocess and feed it the payload over STDIN."""

from __future__ import annotations

import asyncio
import json
from typing import Any


async def spawn_worker(
    python_executable: str, payload: dict[str, Any]
) -> asyncio.subprocess.Process:
    """Start ``python -m gallery_dl_web.gallerydl.worker`` and write the JSON payload to its stdin.

    Uses ``create_subprocess_exec`` (not shell) so the cookie-bearing payload is never interpreted
    by a shell. The payload is written and stdin is closed before returning; the worker then runs
    to completion writing JSON-lines to stdout.
    """
    proc = await asyncio.create_subprocess_exec(
        python_executable,
        "-m",
        "gallery_dl_web.gallerydl.worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdin = proc.stdin
    assert stdin is not None
    stdin.write(json.dumps(payload).encode("utf-8"))
    await stdin.drain()
    stdin.close()
    await stdin.wait_closed()
    return proc
