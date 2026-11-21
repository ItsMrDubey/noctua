"""Runs collectors with timeouts, exponential backoff with jitter and a circuit breaker."""

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from osint.core.collector import BaseCollector, PollCollector, StreamCollector
from osint.core.geo import utcnow
from osint.core.metrics import Metrics
from osint.core.models import Batch

log = logging.getLogger(__name__)


@dataclass
class SourceHealth:
    status: str = "starting"  # starting | healthy | degraded | down
    consecutive_failures: int = 0
    last_success: datetime | None = None
    last_error: str | None = None
    last_batch_size: int = 0


class Scheduler:
    def __init__(
        self,
        collectors: list[BaseCollector],
        ctx: object,
        submit: Callable[[Batch], Awaitable[None]],
        metrics: Metrics,
        *,
        base_backoff_s: float = 1.0,
        max_backoff_s: float = 600.0,
        breaker_threshold: int = 5,
        breaker_cooldown_s: float = 600.0,
        poll_timeout_s: float = 120.0,
    ) -> None:
        self.collectors = collectors
        self.ctx = ctx
        self.submit = submit
        self.metrics = metrics
        self.base_backoff_s = base_backoff_s
        self.max_backoff_s = max_backoff_s
        self.breaker_threshold = breaker_threshold
        self.breaker_cooldown_s = breaker_cooldown_s
        self.poll_timeout_s = poll_timeout_s
        self.health = {c.name: SourceHealth() for c in collectors}
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        for c in self.collectors:
            if isinstance(c, PollCollector):
                coro = self._run_poll(c)
            elif isinstance(c, StreamCollector):
                coro = self._run_stream(c)
            else:
                raise TypeError(f"{c.name} is neither a poll nor a stream collector")
            self._tasks.append(asyncio.create_task(coro, name=f"collector:{c.name}"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()

    def _failure_delay(self, failures: int) -> float:
        if failures >= self.breaker_threshold:
            return self.breaker_cooldown_s
        delay = min(self.max_backoff_s, self.base_backoff_s * 2 ** (failures - 1))
        return delay * random.uniform(0.5, 1.0)

    def _ok(self, c: BaseCollector, batch: Batch, duration_s: float) -> None:
        h = self.health[c.name]
        h.status, h.consecutive_failures = "healthy", 0
        h.last_success, h.last_batch_size = utcnow(), batch.size()
        self.metrics.inc("collector_runs_total", source=c.name, result="ok")
        self.metrics.inc("records_collected_total", batch.size(), source=c.name)
        self.metrics.set("collector_last_duration_seconds", duration_s, source=c.name)

    def _fail(self, c: BaseCollector, exc: BaseException) -> float:
        h = self.health[c.name]
        h.consecutive_failures += 1
        h.status = "down" if h.consecutive_failures >= self.breaker_threshold else "degraded"
        h.last_error = f"{type(exc).__name__}: {exc}"[:300]
        self.metrics.inc("collector_runs_total", source=c.name, result="error")
        log.warning("collector %s failed (%d in a row): %s", c.name, h.consecutive_failures,
                    h.last_error)
        return self._failure_delay(h.consecutive_failures)

    async def _run_poll(self, c: PollCollector) -> None:
        while True:
            t0 = time.monotonic()
            try:
                batch = await asyncio.wait_for(c.poll(self.ctx), timeout=self.poll_timeout_s)
                await self.submit(batch)
                self._ok(c, batch, time.monotonic() - t0)
                delay = c.interval_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — isolate every collector failure
                delay = self._fail(c, exc)
            await asyncio.sleep(delay)

    async def _run_stream(self, c: StreamCollector) -> None:
        while True:
            t0 = time.monotonic()
            try:
                async for batch in c.stream(self.ctx):
                    await self.submit(batch)
                    self._ok(c, batch, time.monotonic() - t0)
                    t0 = time.monotonic()
                raise ConnectionError("stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await asyncio.sleep(self._fail(c, exc))
