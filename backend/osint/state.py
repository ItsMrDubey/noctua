"""Worker state snapshot: the worker publishes it (data/run/state.msgpack, ~1 s), the API
process reads it to serve live positions, health, metrics and job status."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import msgspec

from osint.core.models import Position

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# job status dicts hit SQLite (call counts, verdicts): refreshed at most this often
STATUS_INTERVAL_S = 5.0
REPLACE_ATTEMPTS = 20
REPLACE_DELAY_S = 0.025


def state_path(data_dir: Path) -> Path:
    return data_dir / "run" / "state.msgpack"


class LivePos(Position, array_like=True, frozen=True):
    """A Position encoded as an array (half the size of a map; isinstance Position)."""


class SourceRow(msgspec.Struct, array_like=True):
    status: str
    consecutive_failures: int
    last_success: datetime | None
    last_error: str | None
    last_batch_size: int


class WorkerSnapshot(msgspec.Struct, kw_only=True):
    version: int
    pid: int
    heartbeat: float  # unix seconds
    booting: bool
    boot_error: str | None
    ingestor: dict[str, Any]  # alive, errors, queue_depth, consecutive_flush_failures
    scheduler: dict[str, SourceRow]
    metrics: str  # prometheus text
    jobs: dict[str, Any]  # plain-dict status per job (absent = job not running)
    live: list[LivePos]


_DECODER = msgspec.msgpack.Decoder(WorkerSnapshot)


def _ns_dict(obj: object) -> dict | None:
    return None if obj is None else msgspec.to_builtins(obj.__dict__)


def job_status(runtime, with_slow: bool) -> dict[str, Any]:
    """Plain-dict status of every job the API exposes. ``with_slow`` includes the
    SQLite-backed extraction/translation status (the caller reuses the last ones otherwise)."""
    jobs: dict[str, Any] = {}
    builder = getattr(runtime, "event_builder", None)
    if builder is not None:
        corr = getattr(builder, "corroborator", None)
        jobs["event_builder"] = {
            "passes": builder.passes, "last_error": builder.last_error,
            "last_pass": _ns_dict(builder.last_pass),
            "corroborator_last": _ns_dict(getattr(corr, "last", None)),
        }
    det = getattr(runtime, "sensor_events", None)
    if det is not None:
        jobs["sensor_events"] = msgspec.to_builtins(
            {"passes": det.passes, "last": det.last, "last_error": det.last_error})
    if with_slow:
        for key, attr in (("extraction", "extraction_job"), ("translate", "translate_job")):
            job = getattr(runtime, attr, None)
            if job is not None:
                try:
                    jobs[key] = msgspec.to_builtins(job.status())
                except Exception as exc:
                    log.exception("%s status failed", key)
                    jobs[key] = {"status_error": f"{type(exc).__name__}: {exc}"}
    return jobs


class StatePublisher:
    """Writes the runtime's WorkerSnapshot atomically every ``interval_s`` (encode and
    write run in a thread; one publish at a time, so a slow one delays the next)."""

    def __init__(self, runtime, path: Path, interval_s: float = 1.0,
                 status_interval_s: float = STATUS_INTERVAL_S) -> None:
        self.rt = runtime
        self.path = path
        self.interval_s = interval_s
        self.status_interval_s = status_interval_s
        self._enc = msgspec.msgpack.Encoder()
        self._slow: dict[str, Any] = {}
        self._slow_at = float("-inf")
        self.publishes = 0
        self.last_seconds = 0.0
        self.last_bytes = 0

    def snapshot(self) -> WorkerSnapshot:
        rt = self.rt
        ing = rt.ingestor
        with ing._lock:
            live = list(ing.live.values())
        rows = [(p.track_id, p.kind, p.ts, p.lat, p.lon, p.source, p.alt_ft, p.speed_kts,
                 p.heading_deg) for p in live]
        now = time.monotonic()
        with_slow = now - self._slow_at >= self.status_interval_s or any(
            getattr(rt, attr, None) is not None and key not in self._slow
            for key, attr in (("extraction", "extraction_job"), ("translate", "translate_job")))
        jobs = job_status(rt, with_slow)
        if with_slow:
            self._slow_at = now
            self._slow = {k: jobs[k] for k in ("extraction", "translate") if k in jobs}
        else:
            jobs.update(self._slow)
        health = {name: tuple(asdict(h).values()) for name, h in rt.scheduler.health.items()}
        return WorkerSnapshot(
            version=SCHEMA_VERSION, pid=os.getpid(), heartbeat=time.time(),
            booting=rt.booting, boot_error=rt.boot_error,
            ingestor={"alive": rt.ingestor_alive, "errors": ing.errors,
                      "queue_depth": ing.queue.qsize(),
                      "consecutive_flush_failures": getattr(ing, "consecutive_flush_failures", 0)},
            scheduler=health,  # tuples encode like SourceRow arrays
            metrics=rt.metrics.render_prometheus(), jobs=jobs,
            live=rows,  # tuples encode like LivePos arrays
        )

    def publish_once(self) -> int:
        """Encode and atomically replace the state file; returns the size in bytes."""
        t0 = time.perf_counter()
        data = self._enc.encode(self.snapshot())
        write_atomic(self.path, data)
        self.publishes += 1
        self.last_seconds = time.perf_counter() - t0
        self.last_bytes = len(data)
        self.rt.metrics.set("state_publish_seconds", self.last_seconds)
        self.rt.metrics.set("state_bytes", self.last_bytes)
        return len(data)

    async def run(self) -> None:
        while True:
            t0 = time.monotonic()
            try:
                await asyncio.to_thread(self.publish_once)
            except Exception:
                log.exception("state publish failed")
            await asyncio.sleep(max(0.05, self.interval_s - (time.monotonic() - t0)))


def write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a temp file + os.replace (retried briefly: on Windows
    the replace fails while a reader holds the target open)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_DELAY_S)


class WorkerState:
    """Reader of the worker's state file: reloads when its mtime/size changes, checking at
    most every ``min_interval_s`` seconds. ``get()`` is the last good snapshot or None.
    One caller decodes at a time; concurrent callers get the previous snapshot meanwhile."""

    def __init__(self, path: Path, min_interval_s: float = 0.5, clock=time.monotonic) -> None:
        self.path = path
        self.min_interval_s = min_interval_s
        self.clock = clock
        self._reload = threading.Lock()
        self._checked = float("-inf")
        self._sig: tuple[int, int] | None = None
        # (snapshot, live by track id), replaced as a whole (never mutated)
        self._cur: tuple[WorkerSnapshot | None, dict[str, Position]] = (None, {})

    def _refresh(self) -> None:
        if self.clock() - self._checked < self.min_interval_s:
            return
        if not self._reload.acquire(blocking=False):
            return
        try:
            self._checked = self.clock()
            try:
                st = self.path.stat()
            except FileNotFoundError:
                self._sig, self._cur = None, (None, {})
                return
            sig = (st.st_mtime_ns, st.st_size)
            if sig == self._sig:
                return
            try:
                snap = _DECODER.decode(self.path.read_bytes())
            except (OSError, msgspec.DecodeError) as exc:  # replaced mid-read / newer schema
                log.warning("worker state unreadable: %s", exc)
                return
            if snap.version != SCHEMA_VERSION:
                log.warning("worker state schema %s, expected %s", snap.version, SCHEMA_VERSION)
                return
            self._sig, self._cur = sig, (snap, {p.track_id: p for p in snap.live})
        finally:
            self._reload.release()

    def get(self) -> WorkerSnapshot | None:
        self._refresh()
        return self._cur[0]

    def live(self) -> dict[str, Position]:
        """track id -> position; replaced (never mutated) on reload."""
        self._refresh()
        return self._cur[1]

    def heartbeat_age(self, now: float | None = None) -> float | None:
        snap = self.get()
        if snap is None:
            return None
        return max(0.0, (time.time() if now is None else now) - snap.heartbeat)
