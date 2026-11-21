import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from osint.api.app import create_app
from osint.config import Settings
from osint.core.collector import BaseCollector, PollCollector
from osint.core.geo import utcnow
from osint.core.models import Batch, Observation, Position
from osint.runtime import Runtime


class FakeCollector(PollCollector):
    name, domain, licence, commercial_ok = "fake", "test", "CC0", True
    default_interval_s = 0.05

    async def poll(self, ctx):
        ts = utcnow() - timedelta(seconds=5)
        return Batch(
            source=self.name,
            positions=[Position(track_id="air:f1", kind="air", ts=ts, lat=1, lon=2, source="fake")],
            observations=[Observation(id="fire:f", type="fire", ts=ts, lat=1, lon=2, source="fake")],
        )


def test_app_runs_collectors_and_reports(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    runtime = Runtime(settings, [FakeCollector({})])
    with TestClient(create_app(runtime)) as client:
        for _ in range(100):
            if client.get("/health").json()["sources"]["fake"]["status"] == "healthy":
                break
            time.sleep(0.05)
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert health["sources"]["fake"]["status"] == "healthy"
        assert client.get("/api/live/summary").json() == {"tracks": {"air": 1}, "quarantined": 0}
        assert client.get("/api/sources").json()[0]["name"] == "fake"
        assert 'collector_runs_total{result="ok",source="fake"}' in client.get("/metrics").text
    # leaving the context runs shutdown, which flushes the ingestor
    assert runtime.lake.query("SELECT count(*) FROM observations") == [(1,)]
    assert runtime.lake.query("SELECT count(*) FROM positions") == [(1,)]


class FailingCollector(PollCollector):
    name, domain, licence, commercial_ok = "failing", "test", "CC0", True

    async def poll(self, ctx):
        raise ConnectionError("upstream down")


class NotACollector(BaseCollector):
    name, domain, licence, commercial_ok = "weird", "test", "CC0", True


def _wait(client, pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        health = client.get("/health").json()
        if pred(health):
            return health
        assert time.monotonic() < deadline, f"health never matched: {health}"
        time.sleep(0.02)


def test_health_reports_ingestor(tmp_path):
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path), [FakeCollector({})])
    with TestClient(create_app(runtime)) as client:
        health = _wait(client, lambda h: h["sources"]["fake"]["status"] == "healthy")
        assert health["status"] == "ok"
        assert health["ingestor"]["alive"] is True
        assert health["ingestor"]["errors"] == 0
        assert health["ingestor"]["queue_depth"] >= 0


def test_health_degraded_when_ingestor_dead(tmp_path, monkeypatch):
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path), [FakeCollector({})])

    async def dying_run():
        raise RuntimeError("ingestor crashed")

    monkeypatch.setattr(runtime.ingestor, "run", dying_run)
    with TestClient(create_app(runtime)) as client:
        health = _wait(client, lambda h: h.get("ingestor", {}).get("alive") is False)
        assert health["status"] == "degraded"


def test_health_degraded_when_all_sources_down(tmp_path):
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path), [FailingCollector({})])
    runtime.scheduler.base_backoff_s = 0.001
    runtime.scheduler.breaker_threshold = 2
    with TestClient(create_app(runtime)) as client:
        health = _wait(client, lambda h: h["sources"]["failing"]["status"] == "down")
        assert health["status"] == "degraded"
        assert health["ingestor"]["alive"] is True


def test_lifespan_stops_runtime_when_start_fails(tmp_path):
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path), [NotACollector({})])
    with pytest.raises(TypeError, match="weird"), TestClient(create_app(runtime)):
        pass
    assert runtime.http.is_closed
    assert not runtime.ingestor_alive
