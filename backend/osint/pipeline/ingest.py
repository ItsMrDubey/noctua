"""Ingest stage: validate, quarantine, thin, dedupe, buffer and flush to the lake."""

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import msgspec

from osint.core.geo import utcnow, valid_coord
from osint.core.metrics import Metrics
from osint.core.models import Batch, Document, Position
from osint.store.lake import Lake, document_row, observation_row, position_row
from osint.store.sqlite import SqliteStore

if TYPE_CHECKING:
    from osint.pipeline.triage import Triage

log = logging.getLogger(__name__)

DEFAULT_THIN_S = {"air": 60.0, "sea": 300.0}
_MAX_FUTURE = timedelta(minutes=10)
DOC_MAX_AGE = timedelta(hours=48)  # older documents are stale: counted and skipped


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
        max_buffer_rows: int = 500_000,
        triage: "Triage | None" = None,
        doc_max_age: timedelta = DOC_MAX_AGE,
    ) -> None:
        self.store, self.lake, self.metrics = store, lake, metrics
        self.thin_s = thin_s or DEFAULT_THIN_S
        self.flush_interval_s = flush_interval_s
        self.flush_rows = flush_rows
        self.max_buffer_rows = max_buffer_rows
        self.consecutive_flush_failures = 0
        self._drop_warned = False
        self.queue: asyncio.Queue[Batch] = asyncio.Queue(maxsize=queue_size)
        self.live: dict[str, Position] = {}
        self._buffers: dict[str, list[dict]] = {
            "positions": [], "observations": [], "documents": []}
        self._last_written: dict[str, datetime] = {}
        self._seen_obs: dict[tuple[str, str], datetime] = {}
        # document id -> fetched_at; kept >= doc_max_age so a stale-skip covers expiry
        self._seen_docs: dict[str, datetime] = {}
        # Runtime passes a settings-aware Triage and preloads it; else created lazily
        self._triage = triage
        self.doc_max_age = doc_max_age
        self._last_flush = time.monotonic()
        self._task: asyncio.Task | None = None
        self._inflight: asyncio.Future | None = None
        # serialises all mutation of ingestor state (worker thread vs loop thread)
        self._lock = threading.Lock()
        self.errors = 0

    async def submit(self, batch: Batch) -> None:
        await self.queue.put(batch)
        self.metrics.set("ingest_queue_depth", self.queue.qsize())

    def warm_start(self, since: datetime) -> None:
        index = self.lake.recent_observation_index(since)
        doc_index = self.lake.recent_document_index(since)
        with self._lock:
            for key, ts in index.items():
                current = self._seen_obs.get(key)
                if current is None or ts > current:
                    self._seen_obs[key] = ts
            for doc_id, ts in doc_index.items():
                current = self._seen_docs.get(doc_id)
                if current is None or ts > current:
                    self._seen_docs[doc_id] = ts

    @property
    def triage(self) -> "Triage":
        if self._triage is None:
            from osint.pipeline.triage import Triage

            self._triage = Triage.default()
        return self._triage

    def _bad(self, batch: Batch, record_type: str, record: object, reason: str) -> None:
        self.store.quarantine(batch.source, record_type, reason, msgspec.to_builtins(record))
        self.metrics.inc("records_quarantined_total", source=batch.source, type=record_type)

    @staticmethod
    def _naive(ts: datetime) -> bool:
        return ts.tzinfo is None or ts.utcoffset() is None

    def process(self, batch: Batch) -> None:
        with self._lock:
            self._process(batch)

    def _process(self, batch: Batch) -> None:
        now = utcnow()
        self.store.upsert_tracks(batch.tracks, now)
        for p in batch.positions:
            if not valid_coord(p.lat, p.lon):
                self._bad(batch, "position", p, "invalid coordinates")
                continue
            if self._naive(p.ts):
                self._bad(batch, "position", p, "naive timestamp")
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
            if self._naive(o.ts):
                self._bad(batch, "observation", o, "naive timestamp")
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
        for d in batch.documents:
            self._process_document(batch, d, now)
        self._enforce_cap()
        self.metrics.inc("batches_ingested_total", source=batch.source)
        self.metrics.set("live_tracks", len(self.live))

    def _process_document(self, batch: Batch, d: Document, now: datetime) -> None:
        """Validate, dedupe and triage one document (caller holds the lock)."""
        if not d.url or not d.id:
            self._bad(batch, "document", d, "missing url or id")
            return
        if self._naive(d.published_at) or self._naive(d.fetched_at):
            self._bad(batch, "document", d, "naive timestamp")
            return
        if d.published_at > now + _MAX_FUTURE:
            self._bad(batch, "document", d, "timestamp in the future")
            return
        if d.published_at < now - self.doc_max_age:
            self.metrics.inc("documents_stale_total", source=batch.source)
            return
        if d.id in self._seen_docs:
            self.metrics.inc("documents_duplicate_total", source=batch.source)
            return
        result = self.triage.assess(d)  # pure CPU; we are already in the worker thread
        self._seen_docs[d.id] = d.fetched_at
        self.metrics.inc("documents_triaged_total", source=batch.source)
        if result.relevance >= self.triage.cfg.relevant:
            for category in result.categories or ["uncategorised"]:
                self.metrics.inc("documents_relevant_total", category=category)
        self._buffers["documents"].append(document_row(d, result))

    def _buffered(self) -> int:
        return sum(len(rows) for rows in self._buffers.values())

    def _enforce_cap(self) -> None:
        """Deliberate, counted drop of the oldest rows to bound memory (caller holds lock)."""
        excess = self._buffered() - self.max_buffer_rows
        if excess <= 0:
            return
        dataset = max(self._buffers, key=lambda d: len(self._buffers[d]))
        n = min(excess, len(self._buffers[dataset]))
        del self._buffers[dataset][:n]
        self.metrics.inc("rows_dropped_total", n, dataset=dataset)
        if not self._drop_warned:
            self._drop_warned = True
            log.warning("buffer cap %d exceeded: dropping oldest %s rows (flush failing?)",
                        self.max_buffer_rows, dataset)

    def _flush_delay(self) -> float:
        """Seconds between flush attempts: interval, doubling per failure, capped at 10x."""
        if self.consecutive_flush_failures == 0:
            return self.flush_interval_s
        return min(self.flush_interval_s * 2 ** (self.consecutive_flush_failures - 1),
                   10 * self.flush_interval_s)

    def flush(self) -> None:
        with self._lock:
            try:
                for dataset, rows in self._buffers.items():
                    if rows:
                        self.lake.write(dataset, rows)
                        self.metrics.inc("lake_rows_written_total", len(rows), dataset=dataset)
                        self._buffers[dataset] = []
                self.consecutive_flush_failures = 0
            except BaseException:
                # remaining buffers are kept; retries back off exponentially
                self.consecutive_flush_failures += 1
                raise
            finally:
                self._last_flush = time.monotonic()
                self._drop_warned = False

    def prune(self, older_than: datetime) -> None:
        with self._lock:
            self._seen_obs = {k: ts for k, ts in self._seen_obs.items() if ts >= older_than}
            # documents stay remembered for at least doc_max_age after they were fetched
            doc_cut = min(older_than, utcnow() - self.doc_max_age)
            self._seen_docs = {k: ts for k, ts in self._seen_docs.items() if ts >= doc_cut}
            self._last_written = {
                k: ts for k, ts in self._last_written.items() if ts >= older_than
            }
            self.live = {k: p for k, p in self.live.items() if p.ts >= older_than}

    def prune_live(self, max_age_by_kind: dict[str, timedelta],
                   now: datetime | None = None) -> int:
        """Drop live positions older than their kind's max age (unknown kinds are kept).

        Rebinds ``live`` to a new dict (like ``prune``) so lock-free readers never see the
        dict mutate mid-iteration. Returns the number of entries dropped."""
        now = now or utcnow()
        cutoffs = {kind: now - age for kind, age in max_age_by_kind.items()}
        with self._lock:
            before = len(self.live)
            self.live = {k: p for k, p in self.live.items()
                         if (c := cutoffs.get(p.kind)) is None or p.ts >= c}
            return before - len(self.live)

    def _error(self, what: str, exc: BaseException) -> str:
        with self._lock:
            self.errors += 1
        self.metrics.inc("ingest_errors_total")
        msg = f"{type(exc).__name__}: {exc}"[:300]
        log.error("ingest %s failed: %s", what, msg, exc_info=exc)
        return msg

    def _process_safe(self, batch: Batch) -> None:
        try:
            self.process(batch)
        except Exception as exc:  # noqa: BLE001 — one bad batch must not stop ingestion
            msg = self._error(f"of batch from {batch.source}", exc)
            try:
                self.store.quarantine(batch.source, "batch", f"batch processing failed: {msg}",
                                      {"size": batch.size(), "error": msg})
                self.metrics.inc("records_quarantined_total", source=batch.source, type="batch")
            except Exception:
                log.exception("could not quarantine failed batch from %s", batch.source)

    def _flush_safe(self) -> None:
        try:
            self.flush()
        except Exception as exc:  # noqa: BLE001 — buffers are kept for the next attempt
            self._error(f"flush ({self._buffered()} rows kept for retry)", exc)

    async def _in_thread(self, fn: Callable[..., None], *args: object) -> None:
        """Run ``fn`` in a worker thread. If this task is cancelled the call keeps running,
        so it is tracked in ``self._inflight`` and ``stop()`` waits for it."""
        fut = asyncio.ensure_future(asyncio.to_thread(fn, *args))
        self._inflight = fut
        await asyncio.shield(fut)
        self._inflight = None

    async def run(self) -> None:
        self._task = asyncio.current_task()
        while True:
            remaining = self._flush_delay() - (time.monotonic() - self._last_flush)
            try:
                batch = await asyncio.wait_for(self.queue.get(), timeout=max(remaining, 0.01))
            except TimeoutError:
                batch = None
            if batch is not None:
                await self._in_thread(self._process_safe, batch)
                self.metrics.set("ingest_queue_depth", self.queue.qsize())
            due = time.monotonic() - self._last_flush >= self._flush_delay()
            size_hit = self._buffered() >= self.flush_rows
            # after a failure only the backoff timer may trigger a retry, not buffer size
            if self._buffered() and (due or (size_hit and self.consecutive_flush_failures == 0)):
                await self._in_thread(self._flush_safe)
            elif due:
                self._last_flush = time.monotonic()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("ingestor task had died")
        if self._inflight is not None:  # let a worker-thread call finish before draining
            with contextlib.suppress(Exception):
                await self._inflight
            self._inflight = None
        while not self.queue.empty():
            self._process_safe(self.queue.get_nowait())
        self._flush_safe()
        if self._buffered():
            log.error("ingestor stopped with %d unflushed rows", self._buffered())
