import time
from datetime import timedelta

from fastapi.testclient import TestClient

from osint.api.app import create_app
from osint.config import Settings
from osint.core.collector import PollCollector
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
