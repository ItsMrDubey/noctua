import asyncio
from datetime import timedelta

import pytest

from osint.core.geo import utcnow
from osint.core.metrics import Metrics
from osint.core.models import Batch, Observation, Position, TrackInfo
from osint.pipeline.ingest import Ingestor
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore


@pytest.fixture
def parts(tmp_path):
    store = SqliteStore(tmp_path / "s.sqlite")
    lake = Lake(tmp_path / "lake")
    yield store, lake, Metrics()
    store.close()


def _pos(ts, lat=10.0, track="air:a", kind="air"):
    return Position(track_id=track, kind=kind, ts=ts, lat=lat, lon=20.0, source="t")


def test_positions_thinned_per_track(parts):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics)
    t0 = utcnow() - timedelta(minutes=10)
    ing.process(Batch(source="t", positions=[
        _pos(t0), _pos(t0 + timedelta(seconds=30)), _pos(t0 + timedelta(seconds=61), lat=11.0)]))
    ing.flush()
    assert lake.query("SELECT count(*) FROM positions") == [(2,)]
    assert ing.live["air:a"].lat == 11.0
    assert metrics.get("positions_thinned_total", source="t") == 1


def test_invalid_records_quarantined(parts):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics)
    ing.process(Batch(source="t", positions=[_pos(utcnow(), lat=95.0)],
                      observations=[Observation(id="x", type="fire", ts=utcnow() + timedelta(days=1),
                                                lat=1, lon=1, source="t")]))
    ing.flush()
    assert store.quarantine_count() == 2
    assert lake.query("SELECT count(*) FROM positions") == [(0,)]


def test_observations_deduped_by_id_and_version(parts):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics)
    ts = utcnow() - timedelta(minutes=5)
    o1 = Observation(id="quake:1", version="a", type="quake", ts=ts, lat=1, lon=1, source="t")
    o2 = Observation(id="quake:1", version="b", type="quake", ts=ts, lat=1, lon=1, source="t")
    ing.process(Batch(source="t", observations=[o1, o1, o2]))
    ing.flush()
    assert lake.query("SELECT count(*) FROM observations") == [(2,)]


def test_warm_start_skips_already_stored(parts):
    store, lake, metrics = parts
    ts = utcnow() - timedelta(minutes=5)
    o = Observation(id="fire:1", type="fire", ts=ts, lat=1, lon=1, source="t")
    first = Ingestor(store, lake, metrics)
    first.process(Batch(source="t", observations=[o]))
    first.flush()
    second = Ingestor(store, lake, metrics)
    second.warm_start(utcnow() - timedelta(hours=48))
    second.process(Batch(source="t", observations=[o]))
    second.flush()
    assert lake.query("SELECT count(*) FROM observations") == [(1,)]


def test_tracks_upserted(parts):
    store, lake, metrics = parts
    Ingestor(store, lake, metrics).process(
        Batch(source="t", tracks=[TrackInfo(track_id="sea:1", kind="sea", ident="1", name="X")]))
    assert store.get_track("sea:1")["name"] == "X"


async def test_run_and_stop_flush_everything(parts):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics, flush_interval_s=60)
    task = asyncio.create_task(ing.run())
    await ing.submit(Batch(source="t", positions=[_pos(utcnow())]))
    await asyncio.sleep(0.05)
    await ing.stop()
    assert task.done()
    assert lake.query("SELECT count(*) FROM positions") == [(1,)]
