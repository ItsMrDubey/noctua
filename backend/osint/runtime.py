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
from osint.pipeline.triage import Triage
from osint.store.lake import SCHEMAS, Lake
from osint.store.sqlite import SqliteStore

log = logging.getLogger(__name__)

DEDUPE_WINDOW = timedelta(hours=48)
USER_AGENT = "osint-poc/0.1"
# how long a live position stays "current" per kind; shared with the map layers and counts
LIVE_MAX_AGE = {"air": timedelta(minutes=30), "sea": timedelta(hours=6)}
# closed lake partitions (hour ended this long ago) are compacted by maintenance
COMPACT_GRACE = timedelta(minutes=10)
# WAL checkpoint(TRUNCATE) cadence of the events database (the WAL reached 770 MB)
CHECKPOINT_INTERVAL = timedelta(hours=1)


def live_cutoffs(now: datetime | None = None) -> dict[str, datetime]:
    now = now or utcnow()
    return {kind: now - age for kind, age in LIVE_MAX_AGE.items()}


def is_current(p, cutoffs: dict[str, datetime]) -> bool:
    """True if ``p`` is within its kind's max age (unknown kinds are always current)."""
    cutoff = cutoffs.get(p.kind)
    return cutoff is None or p.ts >= cutoff


class Runtime:
    def __init__(self, settings: Settings, collectors: list[BaseCollector]) -> None:
        self.settings = settings
        self.collectors = collectors
        self.metrics = Metrics()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = SqliteStore(settings.sqlite_path)
        self.lake = Lake(settings.lake_dir)
        self.triage = Triage.default(settings.config_dir / "triage.yaml",
                                     settings.data_dir / "gazetteer")
        self.ingestor = Ingestor(self.store, self.lake, self.metrics, triage=self.triage)
        self.http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30.0
        )
        self.ctx = CollectorContext(http=self.http, settings=settings, store=self.store)
        self.scheduler = Scheduler(collectors, self.ctx, self.ingestor.submit, self.metrics)
        for c in collectors:
            self.store.register_source(c.name, c.domain, c.licence, c.commercial_ok)
        self._tasks: list[asyncio.Task] = []
        self._ingest_task: asyncio.Task | None = None
        self._boot_task: asyncio.Task | None = None
        self.boot_error: str | None = None
        self.imagery_job = None  # set during boot (osint.imagery.job.ImageryJob)
        self.event_builder = None  # set during boot (osint.pipeline.events.EventBuilder)
        self.maintenance_interval_s = 600.0

    @property
    def ingestor_alive(self) -> bool:
        return self._ingest_task is not None and not self._ingest_task.done()

    @property
    def booting(self) -> bool:
        return self._boot_task is not None and not self._boot_task.done()

    async def start(self) -> None:
        # Boot in the background so the API (and the globe) is reachable immediately;
        # the gazetteer load (~30 s) must still finish before the first document batch.
        self._boot_task = asyncio.create_task(self._boot(), name="boot")
        self._tasks.append(self._boot_task)

    async def wait_booted(self) -> None:
        """Wait until background boot has finished (successfully or not)."""
        if self._boot_task is not None:
            await asyncio.wait({self._boot_task})

    async def _boot(self) -> None:
        try:
            await asyncio.gather(
                asyncio.to_thread(self.ingestor.warm_start, utcnow() - DEDUPE_WINDOW),
                asyncio.to_thread(self.triage.preload),
            )
            self._ingest_task = asyncio.create_task(self.ingestor.run(), name="ingestor")
            self._tasks.append(self._ingest_task)
            self._tasks.append(asyncio.create_task(self._maintenance(), name="maintenance"))
            await self.scheduler.start()
            # local import: the events pipeline imports Runtime for typing
            from osint.pipeline.events import wire as wire_events

            self.event_builder = wire_events(self)
            self._tasks.append(
                asyncio.create_task(self.event_builder.run(), name="event_builder"))
            from osint.pipeline.sensor_events import wire as wire_sensor  # sets .sensor_events

            self._tasks.append(asyncio.create_task(wire_sensor(self).run(), name="sensor_events"))
            from osint.imagery.job import wire as wire_imagery

            self.imagery_job = wire_imagery(self)
            self._tasks.append(asyncio.create_task(self.imagery_job.run(), name="imagery"))
            from osint.pipeline.extract.job import start as start_extraction

            self._tasks.append(start_extraction(self))  # sets self.extraction_job
            __import__("osint.naval.job", fromlist=["start"]).start(self)  # sets self.naval_job
            __import__("osint.translate.job", fromlist=["start"]).start(self)  # .translate_job
            __import__("osint.benchmark.job", fromlist=["start"]).start(self)  # .benchmark
            log.info("runtime boot complete")
        except Exception as exc:  # surfaced via /health instead of crashing the app
            self.boot_error = f"{type(exc).__name__}: {exc}"
            log.exception("runtime boot failed")

    async def _maintenance(self) -> None:
        last_checkpoint = utcnow()
        while True:
            await asyncio.sleep(self.maintenance_interval_s)
            if utcnow() - last_checkpoint >= CHECKPOINT_INTERVAL:
                last_checkpoint = utcnow()
                try:
                    res = await asyncio.to_thread(self.store.checkpoint)
                    log.info("sqlite wal checkpoint: %s", res)
                except Exception:
                    log.exception("sqlite wal checkpoint failed")
            try:
                await asyncio.to_thread(self.ingestor.prune, utcnow() - DEDUPE_WINDOW)
                await asyncio.to_thread(self.ingestor.prune_live, LIVE_MAX_AGE)
            except Exception:
                log.exception("maintenance pass failed")
            for dataset in SCHEMAS:
                try:
                    await asyncio.to_thread(self.lake.compact, dataset, utcnow() - COMPACT_GRACE)
                except Exception:
                    log.exception("lake compaction of %s failed", dataset)

    async def stop(self) -> None:
        try:
            if self._boot_task is not None and not self._boot_task.done():
                self._boot_task.cancel()
                await asyncio.wait({self._boot_task})  # never raises; boot logs its own errors
            try:
                await self.scheduler.stop()
            except Exception:
                log.exception("scheduler stop failed")
            for t in list(self._tasks):
                t.cancel()
            for t in list(self._tasks):
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
                self.lake.close()
