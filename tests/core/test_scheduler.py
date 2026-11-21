import asyncio

import pytest

from osint.core.collector import (
    REGISTRY,
    BaseCollector,
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


class OneThenDrop(StreamCollector):
    """Yields one batch per connection then drops: a flapping stream."""

    name, domain, licence, commercial_ok = "flap", "test", "test", True

    def __init__(self, cfg):
        super().__init__(cfg)
        self.connections = 0

    async def stream(self, ctx):
        self.connections += 1
        yield Batch(source=self.name)
        raise ConnectionError("dropped")


class OneThenSilent(StreamCollector):
    name, domain, licence, commercial_ok = "silent", "test", "test", True

    def __init__(self, cfg):
        super().__init__(cfg)
        self.connections = 0

    async def stream(self, ctx):
        self.connections += 1
        yield Batch(source=self.name)
        await asyncio.sleep(3600)


class TrackedStream(StreamCollector):
    """Keeps a reference to its generators so only explicit aclose() can close them."""

    name, domain, licence, commercial_ok = "tracked", "test", "test", True

    def __init__(self, cfg):
        super().__init__(cfg)
        self.gens, self.closed = [], 0

    async def _gen(self):
        try:
            while True:
                yield Batch(source=self.name)
        finally:
            self.closed += 1

    def stream(self, ctx):
        g = self._gen()
        self.gens.append(g)
        return g


class SlowPoll(PollCollector):
    name, domain, licence, commercial_ok = "slowpoll", "test", "test", True

    async def poll(self, ctx):
        await asyncio.sleep(3600)


class NeitherKind(BaseCollector):
    name, domain, licence, commercial_ok = "neither", "test", "test", True


async def test_flapping_stream_opens_breaker():
    c = OneThenDrop({})
    sched, submitted, _ = await _run([c], seconds=0.3, breaker_threshold=3)
    h = sched.health["flap"]
    assert h.status == "down"
    assert h.consecutive_failures == 3
    assert c.connections == 3  # breaker cooldown (60 s) stops further reconnects
    assert len(submitted) == 3


async def test_stream_failures_reset_after_stable_period():
    c = OneThenDrop({})
    sched, _submitted, _ = await _run([c], seconds=0.2, breaker_threshold=3, stream_stable_s=0.0)
    assert sched.health["flap"].status == "degraded"
    assert c.connections > 3


async def test_idle_stream_times_out_and_reconnects():
    c = OneThenSilent({})
    sched, _submitted, _ = await _run([c], seconds=0.5, stream_idle_timeout_s=0.05,
                                      breaker_threshold=100)
    assert c.connections >= 2
    assert sched.health["silent"].last_error == "TimeoutError: (no message)"


async def test_stream_generator_closed_on_stop():
    c = TrackedStream({})
    started = asyncio.Event()

    async def blocking_submit(batch):
        started.set()
        await asyncio.Event().wait()  # backpressure: never returns

    sched = Scheduler([c], ctx=None, submit=blocking_submit, metrics=Metrics())
    await sched.start()
    await asyncio.wait_for(started.wait(), timeout=5)
    await sched.stop()
    assert c.closed == 1


async def test_failing_collector_does_not_affect_healthy_one():
    sched, submitted, _ = await _run([FailPoll({}), OkPoll({})], seconds=0.3, breaker_threshold=3)
    assert sched.health["failpoll"].status == "down"
    assert sched.health["okpoll"].status == "healthy"
    assert sum(b.source == "okpoll" for b in submitted) >= 3


async def test_poll_timeout_recorded_as_failure():
    sched, submitted, metrics = await _run([SlowPoll({})], seconds=0.3, poll_timeout_s=0.02,
                                           breaker_threshold=100)
    h = sched.health["slowpoll"]
    assert h.status == "degraded"
    assert h.consecutive_failures >= 1
    assert h.last_error == "TimeoutError: (no message)"
    assert metrics.get("collector_runs_total", source="slowpoll", result="error") >= 1
    assert submitted == []


async def test_start_does_not_leak_tasks_on_bad_collector():
    submitted = []

    async def submit(batch):
        submitted.append(batch)

    sched = Scheduler([OkPoll({}), NeitherKind({})], ctx=None, submit=submit, metrics=Metrics())
    with pytest.raises(TypeError, match="neither"):
        await sched.start()
    await asyncio.sleep(0.1)
    assert submitted == []
    assert sched._tasks == []
    await sched.stop()


def test_build_collectors_treats_none_cfg_as_empty():
    register(OkPoll)
    try:
        [c] = build_collectors({"okpoll": None})
        assert c.cfg == {}
    finally:
        REGISTRY.pop("okpoll", None)
