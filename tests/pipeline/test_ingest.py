import asyncio
import json
import threading
from datetime import datetime, timedelta

import pytest

from osint.core.geo import utcnow
from osint.core.metrics import Metrics
from osint.core.models import Batch, Observation, Position, TrackInfo
from osint.pipeline.ingest import Ingestor
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore
from tests.util import eventually


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


def test_flush_records_when_each_observation_type_landed(parts):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics)
    ts = utcnow() - timedelta(minutes=5)
    ing.flush()
    assert ing.flushed == {}  # nothing buffered: nothing landed
    ing.process(Batch(source="t", positions=[_pos(utcnow())], observations=[
        Observation(id="fire:1", type="fire", ts=ts, lat=1, lon=1, source="t")]))
    ing.flush()
    assert set(ing.flushed) == {"fire"}  # positions are pushed live, not versioned
    first = ing.flushed["fire"]
    ing.process(Batch(source="t", observations=[
        Observation(id="quake:1", type="quake", ts=ts, lat=1, lon=1, source="t")]))
    ing.flush()
    assert ing.flushed["fire"] == first and ing.flushed["quake"] >= first


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


def _quarantine_rows(store):
    with store._lock:
        return [tuple(r) for r in store._db.execute(
            "SELECT source, record_type, reason, payload FROM quarantine ORDER BY id").fetchall()]


async def test_stop_waits_for_in_flight_process(parts, monkeypatch):
    """stop() must not drain/flush while a worker thread is still inside process()."""
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics, flush_interval_s=60)
    entered, release = threading.Event(), threading.Event()
    real_upsert = store.upsert_tracks

    def slow_upsert(tracks, seen):
        entered.set()
        release.wait(5)
        real_upsert(tracks, seen)

    monkeypatch.setattr(store, "upsert_tracks", slow_upsert)
    task = asyncio.create_task(ing.run())
    await ing.submit(Batch(source="t", positions=[_pos(utcnow())]))
    await eventually(entered.is_set)
    asyncio.get_running_loop().call_later(0.2, release.set)
    await ing.stop()
    assert task.done()
    assert lake.query("SELECT count(*) FROM positions") == [(1,)]


async def test_prune_is_serialised_with_process(parts, monkeypatch):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics)
    entered, release = threading.Event(), threading.Event()

    def slow_upsert(tracks, seen):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(store, "upsert_tracks", slow_upsert)
    proc = asyncio.create_task(asyncio.to_thread(
        ing.process, Batch(source="t", positions=[_pos(utcnow())])))
    await eventually(entered.is_set)
    pruned = asyncio.create_task(asyncio.to_thread(ing.prune, utcnow() - timedelta(hours=1)))
    await asyncio.sleep(0.1)
    assert not pruned.done()  # blocked behind the in-flight process()
    release.set()
    await asyncio.wait_for(asyncio.gather(proc, pruned), timeout=5)
    assert "air:a" in ing.live


def test_naive_timestamps_quarantined(parts):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics)
    naive = datetime(2026, 11, 21, 12, 0)
    ing.process(Batch(source="t", positions=[_pos(naive)],
                      observations=[Observation(id="x", type="fire", ts=naive, lat=1, lon=1,
                                                source="t")]))
    ing.flush()
    rows = _quarantine_rows(store)
    assert [(r[1], r[2]) for r in rows] == [("position", "naive timestamp"),
                                           ("observation", "naive timestamp")]
    assert lake.query("SELECT count(*) FROM positions") == [(0,)]


async def test_run_survives_process_error(parts, monkeypatch):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics, flush_interval_s=60)
    real_upsert = store.upsert_tracks

    def flaky_upsert(tracks, seen):
        if tracks:
            raise RuntimeError("db exploded")
        real_upsert(tracks, seen)

    monkeypatch.setattr(store, "upsert_tracks", flaky_upsert)
    task = asyncio.create_task(ing.run())
    await ing.submit(Batch(source="bad", tracks=[TrackInfo(track_id="sea:1", kind="sea",
                                                           ident="1")]))
    await eventually(lambda: metrics.get("ingest_errors_total") == 1)
    await ing.submit(Batch(source="t", positions=[_pos(utcnow())]))
    await eventually(lambda: metrics.get("batches_ingested_total", source="t") == 1)
    assert not task.done()
    assert ing.errors == 1
    await ing.stop()
    assert lake.query("SELECT count(*) FROM positions") == [(1,)]
    [(source, record_type, reason, payload)] = _quarantine_rows(store)
    assert (source, record_type) == ("bad", "batch")
    assert "db exploded" in reason
    assert json.loads(payload) == {"size": 1, "error": "RuntimeError: db exploded"}


async def test_run_keeps_buffers_and_retries_failed_flush(parts, monkeypatch):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics, flush_interval_s=0.05)
    real_write = lake.write
    calls = []

    def flaky_write(dataset, rows):
        calls.append(dataset)
        if len(calls) == 1:
            raise OSError("disk full")
        return real_write(dataset, rows)

    monkeypatch.setattr(lake, "write", flaky_write)
    task = asyncio.create_task(ing.run())
    await ing.submit(Batch(source="t", positions=[_pos(utcnow())]))
    await eventually(lambda: real_count(lake) == 1)
    assert not task.done()
    assert metrics.get("ingest_errors_total") == 1
    await ing.stop()
    assert real_count(lake) == 1


def real_count(lake):
    return lake.query("SELECT count(*) FROM positions")[0][0]


def test_warm_start_keys_survive_prune_until_real_ts_expires(parts):
    store, lake, metrics = parts
    o = Observation(id="fire:1", type="fire", ts=utcnow() - timedelta(minutes=5), lat=1, lon=1,
                    source="t")
    first = Ingestor(store, lake, metrics)
    first.process(Batch(source="t", observations=[o]))
    first.flush()
    second = Ingestor(store, lake, metrics)
    second.warm_start(utcnow() - timedelta(hours=48))
    second.prune(utcnow() - timedelta(hours=47))  # a maintenance pass some time later
    second.process(Batch(source="t", observations=[o]))
    second.flush()
    assert lake.query("SELECT count(*) FROM observations") == [(1,)]


async def test_persistent_flush_failure_is_backed_off(parts, monkeypatch):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics, flush_interval_s=0.2, flush_rows=1)
    calls = []

    def failing_write(dataset, rows):
        calls.append(dataset)
        raise OSError("disk full")

    monkeypatch.setattr(lake, "write", failing_write)
    task = asyncio.create_task(ing.run())
    for i in range(40):
        await ing.submit(Batch(source="t", positions=[_pos(utcnow(), track=f"air:{i}")]))
        await asyncio.sleep(0.005)
    await eventually(lambda: ing.queue.empty())
    task.cancel()
    # size trigger fires once, then the batches are held back by the backoff window
    assert 1 <= len(calls) <= 2
    assert ing.consecutive_flush_failures == len(calls)
    await asyncio.gather(task, return_exceptions=True)


def test_backoff_doubles_capped_and_resets_on_success(parts, monkeypatch):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics, flush_interval_s=1.0)
    real_write = lake.write
    fail = {"on": True}

    def write(dataset, rows):
        if fail["on"]:
            raise OSError("disk full")
        return real_write(dataset, rows)

    monkeypatch.setattr(lake, "write", write)
    ing.process(Batch(source="t", positions=[_pos(utcnow())]))
    delays = []
    for _ in range(8):
        with pytest.raises(OSError):
            ing.flush()
        delays.append(ing._flush_delay())
    assert delays == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0, 10.0]
    assert ing.consecutive_flush_failures == 8
    fail["on"] = False
    ing.flush()
    assert ing.consecutive_flush_failures == 0
    assert ing._flush_delay() == 1.0
    assert real_count(lake) == 1


def test_buffer_cap_drops_oldest_and_counts(parts, caplog):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics, max_buffer_rows=5)
    t0 = utcnow() - timedelta(hours=1)
    with caplog.at_level("WARNING"):
        for i in range(3):
            ing.process(Batch(source="t", positions=[
                _pos(t0 + timedelta(minutes=i), track=f"air:{2 * i}"),
                _pos(t0 + timedelta(minutes=i), track=f"air:{2 * i + 1}")]))
    assert ing._buffered() == 5
    assert metrics.get("rows_dropped_total", dataset="positions") == 1
    assert ing._buffers["positions"][0]["track_id"] == "air:1"  # oldest dropped
    assert sum("dropp" in r.message for r in caplog.records) == 1  # once per flush cycle
    ing.flush()
    assert real_count(lake) == 5
