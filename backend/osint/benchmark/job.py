"""Periodic scorecard snapshots (hourly) and the runtime's shared BenchmarkService."""

from __future__ import annotations

import asyncio
import logging

from osint.benchmark.data import Sources
from osint.benchmark.scorecard import BenchmarkService
from osint.benchmark.store import BenchmarkStore

log = logging.getLogger(__name__)


def get_service(runtime) -> BenchmarkService:
    """The runtime's benchmark service, created on first use (shared by job and API)."""
    svc = getattr(runtime, "benchmark", None)
    if svc is None:
        src = Sources.from_settings(runtime.settings, runtime.lake)
        store = BenchmarkStore(runtime.settings.data_dir / "benchmark.sqlite")
        svc = BenchmarkService(src, store)
        runtime.benchmark = svc
    return svc


async def run(svc: BenchmarkService) -> None:
    await asyncio.sleep(svc.cfg.first_snapshot_delay_s)
    while True:
        try:
            n = await asyncio.to_thread(svc.snapshot)
            svc.last_error = None
            log.info("benchmark snapshot: %s", n)
        except Exception as exc:
            svc.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("benchmark snapshot failed")
        await asyncio.sleep(svc.cfg.snapshot_interval_s)


def start(runtime) -> asyncio.Task:
    """Create the background task (one-line wiring from Runtime._boot)."""
    task = asyncio.create_task(run(get_service(runtime)), name="benchmark")
    runtime._tasks.append(task)
    return task
