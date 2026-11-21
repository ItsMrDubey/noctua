"""Ingest stage: validate, quarantine, thin, dedupe, buffer and flush to the lake."""

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timedelta

import msgspec

from osint.core.geo import utcnow, valid_coord
from osint.core.metrics import Metrics
from osint.core.models import Batch, Position
from osint.store.lake import Lake, observation_row, position_row
from osint.store.sqlite import SqliteStore

log = logging.getLogger(__name__)

DEFAULT_THIN_S = {"air": 60.0, "sea": 300.0}
_MAX_FUTURE = timedelta(minutes=10)


class Ingestor:
    def __init__(
        self,
        store: SqliteStore,
        lake: Lake,
        metrics: Metrics,
        *,
        thin_s: dict[str, float] | None = None,
        flush_interval_s: float = 30.0,
        flush_rows: int = 50_000,
        queue_size: int = 64,
    ) -> None:
        self.store, self.lake, self.metrics = store, lake, metrics
        self.thin_s = thin_s or DEFAULT_THIN_S
        self.flush_interval_s = flush_interval_s
        self.flush_rows = flush_rows
        self.queue: asyncio.Queue[Batch] = asyncio.Queue(maxsize=queue_size)
        self.live: dict[str, Position] = {}
        self._buffers: dict[str, list[dict]] = {"positions": [], "observations": []}
        self._last_written: dict[str, datetime] = {}
        self._seen_obs: dict[tuple[str, str], datetime] = {}
        self._last_flush = time.monotonic()
        self._task: asyncio.Task | None = None

    async def submit(self, batch: Batch) -> None:
        await self.queue.put(batch)
        self.metrics.set("ingest_queue_depth", self.queue.qsize())

    def warm_start(self, since: datetime) -> None:
        for key in self.lake.recent_observation_keys(since):
            self._seen_obs[key] = since

    def _bad(self, batch: Batch, record_type: str, record: object, reason: str) -> None:
        self.store.quarantine(batch.source, record_type, reason, msgspec.to_builtins(record))
        self.metrics.inc("records_quarantined_total", source=batch.source, type=record_type)

    def process(self, batch: Batch) -> None:
        now = utcnow()
        self.store.upsert_tracks(batch.tracks, now)
        for p in batch.positions:
            if not valid_coord(p.lat, p.lon):
                self._bad(batch, "position", p, "invalid coordinates")
                continue
            if p.ts > now + _MAX_FUTURE:
                self._bad(batch, "position", p, "timestamp in the future")
                continue
            current = self.live.get(p.track_id)
            if current is None or p.ts >= current.ts:
                self.live[p.track_id] = p
            last = self._last_written.get(p.track_id)
            if last is not None and (p.ts - last).total_seconds() < self.thin_s.get(p.kind, 0.0):
                self.metrics.inc("positions_thinned_total", source=batch.source)
                continue
            self._last_written[p.track_id] = p.ts
            self._buffers["positions"].append(position_row(p))
        for o in batch.observations:
            if not valid_coord(o.lat, o.lon):
                self._bad(batch, "observation", o, "invalid coordinates")
                continue
            if o.ts > now + _MAX_FUTURE:
                self._bad(batch, "observation", o, "timestamp in the future")
                continue
            key = (o.id, o.version)
            if key in self._seen_obs:
                self.metrics.inc("observations_duplicate_total", source=batch.source)
                continue
            self._seen_obs[key] = o.ts
            self._buffers["observations"].append(observation_row(o))
        self.metrics.inc("batches_ingested_total", source=batch.source)
        self.metrics.set("live_tracks", len(self.live))

    def _buffered(self) -> int:
        return sum(len(rows) for rows in self._buffers.values())

    def flush(self) -> None:
        for dataset, rows in self._buffers.items():
            if rows:
                self.lake.write(dataset, rows)
                self.metrics.inc("lake_rows_written_total", len(rows), dataset=dataset)
                self._buffers[dataset] = []
        self._last_flush = time.monotonic()

    def prune(self, older_than: datetime) -> None:
        self._seen_obs = {k: ts for k, ts in self._seen_obs.items() if ts >= older_than}
        self._last_written = {k: ts for k, ts in self._last_written.items() if ts >= older_than}
        self.live = {k: p for k, p in self.live.items() if p.ts >= older_than}

    async def run(self) -> None:
        self._task = asyncio.current_task()
        while True:
            remaining = self.flush_interval_s - (time.monotonic() - self._last_flush)
            try:
                batch = await asyncio.wait_for(self.queue.get(), timeout=max(remaining, 0.01))
            except TimeoutError:
                batch = None
            if batch is not None:
                await asyncio.to_thread(self.process, batch)
                self.metrics.set("ingest_queue_depth", self.queue.qsize())
            due = time.monotonic() - self._last_flush >= self.flush_interval_s
            if self._buffered() and (due or self._buffered() >= self.flush_rows):
                await asyncio.to_thread(self.flush)
            elif due:
                self._last_flush = time.monotonic()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        while not self.queue.empty():
            self.process(self.queue.get_nowait())
        self.flush()
