"""Integration test: spawn the REAL worker subprocess and read its JSON-lines output.

This exercises ``worker_runner.spawn_worker`` and the actual ``worker.main``/``run`` path
(end-to-end, no monkeypatching of gallery-dl). The payload intentionally has no sessionid, so
``config_builder`` raises and the worker emits a ``failed``/``worker-crash`` event and exits 2.
"""

from __future__ import annotations

import json
import sys

from gallery_dl_web.jobs.worker_runner import spawn_worker


async def test_spawn_worker_runs_real_worker(tmp_path) -> None:
    payload = {
        "job_id": "int1",
        "url": "https://www.instagram.com/p/x/",
        "platform": "instagram",
        "output_dir": str(tmp_path),
        "cookies": {},  # missing sessionid -> config_builder raises
        "options": {},
    }
    proc = await spawn_worker(sys.executable, payload)
    events: list[dict] = []
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode().strip()
        if line:
            events.append(json.loads(line))
    rc = await proc.wait()

    assert rc == 2
    assert events, "worker emitted no events"
    assert events[-1]["type"] == "failed"
    assert events[-1]["reason"] == "worker-crash"
