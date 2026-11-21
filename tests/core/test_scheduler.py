import asyncio

import pytest

from osint.core.collector import (
    REGISTRY,
    PollCollector,
    StreamCollector,
    build_collectors,
    register,
)
from osint.core.metrics import Metrics
from osint.core.models import Batch
from osint.core.scheduler import Scheduler


class OkPoll(PollCollector):
    name, domain, licence, commercial_ok = "okpoll", "test", "test", True
    default_interval_s = 0.01

    async def poll(self, ctx):
        return Batch(source=self.name)


class FailPoll(PollCollector):
    name, domain, licence, commercial_ok = "failpoll", "test", "test", True

    async def poll(self, ctx):
        raise ConnectionError("boom")


class TwoThenDie(StreamCollector):
    name, domain, licence, commercial_ok = "stream2", "test", "test", True

    async def stream(self, ctx):
        yield Batch(source=self.name)
        yield Batch(source=self.name)
        raise ConnectionError("socket closed")


async def _run(collectors, seconds=0.15, **kw):
    submitted = []

    async def submit(batch):
        submitted.append(batch)

    metrics = Metrics()
    sched = Scheduler(collectors, ctx=None, submit=submit, metrics=metrics,
                      base_backoff_s=0.001, breaker_cooldown_s=60, **kw)
    await sched.start()
    await asyncio.sleep(seconds)
    await sched.stop()
    return sched, submitted, metrics


async def test_poll_success_marks_healthy():
    sched, submitted, metrics = await _run([OkPoll({})])
    assert len(submitted) >= 3
    assert sched.health["okpoll"].status == "healthy"
    assert metrics.get("collector_runs_total", source="okpoll", result="ok") >= 3


async def test_repeated_failures_open_breaker():
    sched, submitted, _metrics = await _run([FailPoll({})], breaker_threshold=3)
    h = sched.health["failpoll"]
    assert h.status == "down"
    assert h.consecutive_failures == 3  # breaker cooldown (60 s) stops further attempts
    assert "ConnectionError: boom" in h.last_error
    assert submitted == []


async def test_stream_batches_submitted_then_degraded():
    sched, submitted, _ = await _run([TwoThenDie({})], seconds=0.05, breaker_threshold=100)
    assert len(submitted) >= 2
    assert sched.health["stream2"].status == "degraded"


def test_interval_from_cfg():
    assert OkPoll({"interval_s": 5}).interval_s == 5.0
    assert OkPoll({}).interval_s == 0.01


def test_build_collectors_respects_enabled_and_unknown():
    register(OkPoll)
    try:
        assert [c.name for c in build_collectors({"okpoll": {"enabled": True}})] == ["okpoll"]
        assert build_collectors({"okpoll": {"enabled": False}}) == []
        with pytest.raises(KeyError, match="nope"):
            build_collectors({"nope": {}})
    finally:
        REGISTRY.pop("okpoll", None)
