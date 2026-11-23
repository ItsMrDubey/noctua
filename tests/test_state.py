import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import msgspec
from fastapi.testclient import TestClient

from osint.api.app import create_app
from osint.apiruntime import ApiRuntime
from osint.config import Settings
from osint.core.geo import utcnow
from osint.core.models import Position
from osint.core.scheduler import SourceHealth
from osint.pipeline.events import PassStats
from osint.runtime import Runtime
from osint.state import (
    SCHEMA_VERSION,
    StatePublisher,
    WorkerSnapshot,
    WorkerState,
    state_path,
    write_atomic,
)


def _settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path)


def _pos(tid, kind="air", age_s=5.0, **kw):
    return Position(track_id=tid, kind=kind, ts=utcnow() - timedelta(seconds=age_s),
                    lat=1.5, lon=2.5, source="t", **kw)


def _fields(p):
    return msgspec.structs.astuple(p)


class _Job:
    def __init__(self, status):
        self._s = status

    def status(self):
        return self._s


def test_publish_round_trip(tmp_path):
    rt = Runtime(_settings(tmp_path), [])
    try:
        positions = [_pos("air:a", alt_ft=30000, speed_kts=400.0, heading_deg=90.0),
                     _pos("sea:1", kind="sea", age_s=600)]
        rt.ingestor.live.update({p.track_id: p for p in positions})
        ok = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        rt.scheduler.health["adsb"] = SourceHealth("healthy", 0, ok, None, 12)
        rt.scheduler.health["ais"] = SourceHealth("down", 4, None, "boom", 0)
        rt.metrics.inc("collector_runs_total", source="adsb", result="ok")
        rt.event_builder = SimpleNamespace(
            passes=3, last_error=None, last_pass=PassStats(documents=7, skipped={"x": 1}),
            corroborator=SimpleNamespace(last=None))
        rt.sensor_events = SimpleNamespace(passes=2, last={"created": 1}, last_error="e")
        rt.extraction_job = _Job({"enabled": True, "passes": 5})
        path = state_path(tmp_path)
        pub = StatePublisher(rt, path)
        assert pub.publish_once() == path.stat().st_size

        snap = WorkerState(path).get()
        assert snap.version == SCHEMA_VERSION and snap.pid == os.getpid()
        assert abs(snap.heartbeat - time.time()) < 5
        assert snap.ingestor == {"alive": False, "errors": 0, "queue_depth": 0,
                                 "consecutive_flush_failures": 0, "flushed": {}}
        assert 'collector_runs_total{result="ok",source="adsb"}' in snap.metrics

        api = ApiRuntime(_settings(tmp_path), WorkerState(path))
        try:
            live = api.ingestor.live
            assert {k: _fields(v) for k, v in live.items()} == {
                p.track_id: _fields(p) for p in positions}
            assert all(isinstance(p, Position) for p in live.values())
            assert api.scheduler.health == rt.scheduler.health
            # the publish gauges are set after encoding, so they show up one publish later
            assert api.metrics.render_prometheus() == snap.metrics
            assert api.event_builder.passes == 3
            assert api.event_builder.last_pass.__dict__["documents"] == 7
            assert api.event_builder.corroborator.last is None
            assert api.sensor_events.last == {"created": 1}
            assert api.extraction_job.status() == {"enabled": True, "passes": 5}
            assert api.translate_job is None  # the worker does not run it
            assert not api.booting and api.boot_error is None
        finally:
            import asyncio

            asyncio.run(api.stop())
    finally:
        rt.store.close()
        rt.lake.close()


def test_worker_state_reload_throttled_and_missing(tmp_path):
    path = state_path(tmp_path)
    now = [0.0]
    ws = WorkerState(path, min_interval_s=0.5, clock=lambda: now[0])
    assert ws.get() is None and ws.heartbeat_age() is None
    _write(path, live=[_pos("air:a")])
    assert ws.get() is None  # checked at t=0 already
    now[0] = 0.6
    assert set(ws.live()) == {"air:a"}
    _write(path, live=[_pos("air:b")])
    now[0] = 0.7
    assert set(ws.live()) == {"air:a"}
    now[0] = 1.2
    assert set(ws.live()) == {"air:b"}
    path.unlink()
    now[0] = 2.0
    assert ws.get() is None and ws.live() == {}


def _write(path, *, heartbeat=None, booting=False, boot_error=None, live=(), alive=True,
           scheduler=None, jobs=None):
    snap = WorkerSnapshot(
        version=SCHEMA_VERSION, pid=4242, heartbeat=time.time() if heartbeat is None else heartbeat,
        booting=booting, boot_error=boot_error,
        ingestor={"alive": alive, "errors": 0, "queue_depth": 3, "consecutive_flush_failures": 0},
        scheduler=scheduler or {}, metrics="up 1\n", jobs=jobs or {},
        live=[tuple(msgspec.structs.astuple(p)) for p in live])
    write_atomic(path, msgspec.msgpack.encode(snap))
    time.sleep(0.01)  # distinct mtime for the reader


def _client(tmp_path):
    api = ApiRuntime(_settings(tmp_path), WorkerState(state_path(tmp_path), min_interval_s=0))
    return TestClient(create_app(api), base_url="http://127.0.0.1")


def test_api_health_follows_worker_heartbeat(tmp_path):
    path = state_path(tmp_path)
    with _client(tmp_path) as client:
        h = client.get("/health").json()
        assert h["status"] == "worker_down" and h["worker"]["pid"] is None
        _write(path, heartbeat=time.time() - 30)
        h = client.get("/health").json()
        assert h["status"] == "worker_down"
        assert h["worker"]["pid"] == 4242 and h["worker"]["heartbeat_age_s"] >= 29
        _write(path, booting=True)
        h = client.get("/health").json()
        assert h["status"] == "starting" and h["worker"]["booting"] is True
        _write(path, boot_error="ValueError: x")
        assert client.get("/health").json()["status"] == "failed"
        _write(path)
        h = client.get("/health").json()
        assert h["status"] == "ok" and h["ingestor"]["queue_depth"] == 3
        assert h["worker"] == {"pid": 4242, "heartbeat_age_s": h["worker"]["heartbeat_age_s"],
                               "booting": False, "boot_error": None}
        _write(path, alive=False)
        assert client.get("/health").json()["status"] == "degraded"


def test_api_serves_layers_and_status_from_state(tmp_path):
    path = state_path(tmp_path)
    ok = utcnow()
    _write(path, live=[_pos("air:abc", alt_ft=1000), _pos("sea:9", kind="sea")],
           scheduler={"adsb": ("healthy", 0, ok, None, 2)},
           jobs={"translate": {"enabled": True, "running": True, "passes": 9},
                 "event_builder": {"passes": 4, "last_error": None, "last_pass": {"documents": 2},
                                   "corroborator_last": {"evaluated": 5}},
                 "sensor_events": {"passes": 1, "last": None, "last_error": None}})
    with _client(tmp_path) as client:
        body = client.get("/api/layers/aircraft").json()
        assert [i["id"] for i in body["items"]] == ["air:abc"]
        assert client.get("/api/track/air:abc").json()["position"]["alt_ft"] == 1000
        assert client.get("/api/live/summary").json()["tracks"] == {"air": 1, "sea": 1}
        st = client.get("/api/status").json()
        assert st["sources"]["adsb"]["status"] == "healthy"
        assert st["counts"] == {"aircraft": 1, "ships": 1}
        assert client.get("/metrics").text == "up 1\n"
        assert client.get("/api/translate/status").json()["passes"] == 9
        stats = client.get("/api/events/stats").json()
        assert stats["builder"] == {"passes": 4, "last_error": None,
                                    "last_pass": {"documents": 2}}
        assert stats["sensor"]["last_corroboration_pass"] == {"evaluated": 5}
        assert stats["sensor"]["detector"]["passes"] == 1
        ext = client.get("/api/extraction/status").json()
        assert ext["running"] is False and ext["booting"] is False


async def test_publisher_loop_tracks_running_runtime(tmp_path):
    import asyncio

    from tests.util import eventually

    rt = Runtime(_settings(tmp_path), [])
    path = state_path(tmp_path)
    pub = StatePublisher(rt, path, interval_s=0.05)
    await rt.start()
    task = asyncio.create_task(pub.run())
    try:
        ws = WorkerState(path, min_interval_s=0)
        await eventually(lambda: (s := ws.get()) is not None and not s.booting
                         and s.ingestor["alive"], timeout=60)
        assert pub.publishes >= 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await rt.stop()
