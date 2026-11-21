"""Wires stores, ingestor, scheduler and HTTP client into one runnable unit."""

import asyncio
import contextlib
from datetime import timedelta

import httpx

from osint.config import Settings
from osint.core.collector import BaseCollector, CollectorContext
from osint.core.geo import utcnow
from osint.core.metrics import Metrics
from osint.core.scheduler import Scheduler
from osint.pipeline.ingest import Ingestor
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore

DEDUPE_WINDOW = timedelta(hours=48)
USER_AGENT = "osint-poc/0.1"


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

    async def start(self) -> None:
        await asyncio.to_thread(self.ingestor.warm_start, utcnow() - DEDUPE_WINDOW)
        self._tasks.append(asyncio.create_task(self.ingestor.run(), name="ingestor"))
        self._tasks.append(asyncio.create_task(self._maintenance(), name="maintenance"))
        await self.scheduler.start()

    async def _maintenance(self) -> None:
        while True:
            await asyncio.sleep(600)
            self.ingestor.prune(utcnow() - DEDUPE_WINDOW)

    async def stop(self) -> None:
        await self.scheduler.stop()
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await self.ingestor.stop()  # drains the queue and flushes buffers
        await self.http.aclose()
        self.store.close()
