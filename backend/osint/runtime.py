"""Wires stores, ingestor, scheduler and HTTP client into one runnable unit."""

import asyncio
import logging
from datetime import datetime, timedelta

import httpx

from osint.config import Settings
from osint.core.collector import BaseCollector, CollectorContext
from osint.core.geo import utcnow
from osint.core.metrics import Metrics
from osint.core.scheduler import Scheduler
from osint.pipeline.ingest import Ingestor
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore

log = logging.getLogger(__name__)

DEDUPE_WINDOW = timedelta(hours=48)
USER_AGENT = "osint-poc/0.1"
# how long a live position stays "current" per kind; shared with the map layers and counts
LIVE_MAX_AGE = {"air": timedelta(minutes=30), "sea": timedelta(hours=6)}


def live_cutoffs(now: datetime | None = None) -> dict[str, datetime]:
    now = now or utcnow()
    return {kind: now - age for kind, age in LIVE_MAX_AGE.items()}


def is_current(p, cutoffs: dict[str, datetime]) -> bool:
    """True if ``p`` is within its kind's max age (unknown kinds are always current)."""
    cutoff = cutoffs.get(p.kind)
    return cutoff is None or p.ts >= cutoff


def prune_live(ingestor: Ingestor, now: datetime | None = None) -> int:
    """Drop stale entries from ``ingestor.live`` per kind (LIVE_MAX_AGE).

    Coupling note: this reaches into Ingestor internals (``live`` and ``_lock``) because the
    ingestor's own ``prune`` uses the 48 h dedupe window for everything. It rebinds ``live``
    to a new dict (like ``Ingestor.prune``) so lock-free readers never see a dict mutate
    mid-iteration. Candidate to move into Ingestor.prune.
    """
    cutoffs = live_cutoffs(now)
    with ingestor._lock:
        before = len(ingestor.live)
        ingestor.live = {k: p for k, p in ingestor.live.items() if is_current(p, cutoffs)}
        return before - len(ingestor.live)


class Runtime:
    def __init__(self, settings: Settings, collectors: list[BaseCollector]) -> None:
        self.settings = settings
        self.collectors = collectors
        self.metrics = Metrics()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = SqliteStore(settings.sqlite_path)
        self.lake = Lake(settings.lake_dir)
        self.ingestor = Ingestor(self.store, self.lake, self.metrics)
        self.http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30.0
        )
        self.ctx = CollectorContext(http=self.http, settings=settings, store=self.store)
        self.scheduler = Scheduler(collectors, self.ctx, self.ingestor.submit, self.metrics)
        for c in collectors:
            self.store.register_source(c.name, c.domain, c.licence, c.commercial_ok)
        self._tasks: list[asyncio.Task] = []
        self._ingest_task: asyncio.Task | None = None
        self.maintenance_interval_s = 600.0

    @property
    def ingestor_alive(self) -> bool:
        return self._ingest_task is not None and not self._ingest_task.done()

    async def start(self) -> None:
        await asyncio.to_thread(self.ingestor.warm_start, utcnow() - DEDUPE_WINDOW)
        self._ingest_task = asyncio.create_task(self.ingestor.run(), name="ingestor")
        self._tasks.append(self._ingest_task)
        self._tasks.append(asyncio.create_task(self._maintenance(), name="maintenance"))
        await self.scheduler.start()

    async def _maintenance(self) -> None:
        while True:
            await asyncio.sleep(self.maintenance_interval_s)
            try:
                await asyncio.to_thread(self.ingestor.prune, utcnow() - DEDUPE_WINDOW)
                await asyncio.to_thread(prune_live, self.ingestor)
            except Exception:
                log.exception("maintenance pass failed")

    async def stop(self) -> None:
        try:
            try:
                await self.scheduler.stop()
            except Exception:
                log.exception("scheduler stop failed")
            for t in self._tasks:
                t.cancel()
            for t in self._tasks:
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception:
                    log.exception("background task %s had died", t.get_name())
            self._tasks.clear()
            await self.ingestor.stop()  # drains the queue and flushes buffers
        finally:
            try:
                await self.http.aclose()
            finally:
                self.store.close()
