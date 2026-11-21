"""rebuild_events: memory-bounded replay, stable ids, generations, rebuild lock."""

import threading
from datetime import timedelta

from osint.config import ROOT
from osint.core.metrics import Metrics
from osint.pipeline.events import (
    REBUILD_STATE,
    EventBuilder,
    EventsConfig,
    rebuild_events,
    rebuild_lock_path,
)
from osint.store.lake import FileLock, Lake
from osint.store.sqlite import SqliteStore, iso
from tests.pipeline.test_event_builder import NOW, _gdelt_row, _row

CFG = EventsConfig.from_file(ROOT / "config" / "events.yaml")


def _seed_other_origins(store):
    t = iso(NOW - timedelta(days=30))
    store.save_events([{"id": "ucdp-7", "category": "armed_conflict", "title": "u",
                        "t_start": t, "t_end": t, "status": "historical", "confidence": 0.8,
                        "version": 1, "mention_count": 1, "source_diversity": 1,
                        "first_seen": t, "updated_at": t,
                        "cluster_state": {"source": "ucdp"}}])
    store.save_sensor_event(
        {"id": "S1", "category": "air_activity", "title": "orbit", "t_start": t, "t_end": t,
         "status": "sensor-corroborated", "confidence": 0.9, "version": 1,
         "mention_count": 1, "source_diversity": 0, "first_seen": t, "updated_at": t},
        {"detector": "orbit", "dedupe_key": "orbit:k", "cell": "c"}, [],
        {"event_id": "S1", "score": 1.0, "kinds": [], "evidence": 0, "corroborated": True})


def test_rebuild_keeps_ids_and_never_touches_sensor_or_historical(tmp_path):
    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    _seed_other_origins(store)
    lake.write("documents", [
        _row(1, "Missile strike on Kyiv power plant", fetched=NOW - timedelta(hours=5)),
        _row(2, "Missile strike on Kyiv power plant", host="b.example",
             fetched=NOW - timedelta(hours=4, minutes=50))])
    b = EventBuilder(store, lake, Metrics(), CFG, clock=lambda: NOW)
    b.run_once()  # live build: the id the outside world (labels, extraction) knows
    [live] = store.list_events(origin="news", limit=10)
    store.save_verifications([{"event_id": live["id"], "verdict": "supported",
                               "fingerprint": "f"}])
    out = rebuild_events(store, lake, CFG)
    assert out["generation"] == 1 and out["deleted_stale"] == 0
    [ev] = store.list_events(origin="news", limit=10)
    assert ev["id"] == live["id"]
    assert len(store.get_event(ev["id"])["documents"]) == 2  # links re-derived, no dupes
    assert store.verifications([ev["id"]])  # verification still linked
    out = rebuild_events(store, lake, CFG)
    assert out["generation"] == 2
    assert [e["id"] for e in store.list_events(origin="news", limit=10)] == [ev["id"]]
    assert {e["id"] for e in store.list_events(limit=10)} == {ev["id"], "ucdp-7", "S1"}
    assert store.get_cursor(REBUILD_STATE)["generation"] == 2
    store.close()


def test_rebuild_drops_events_it_does_not_reproduce(tmp_path):
    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    lake.write("documents", [
        _row(1, "Missile strike on Kyiv power plant", fetched=NOW - timedelta(hours=5))])
    rebuild_events(store, lake, CFG)
    ghost = {**store.list_events(slim=False, limit=1)[0], "id": "GHOST",
             "cluster_state": {"v": 2, "gen": 1}}
    store.save_events([ghost], documents=[{
        "event_id": "GHOST", "doc_id": "doc:zz", "source": "s", "url": "u", "title": "t",
        "published_at": ghost["t_start"], "role": "primary", "added_at": ghost["t_start"]}])
    out = rebuild_events(store, lake, CFG)
    assert out["deleted_stale"] == 1 and store.get_event("GHOST") is None
    store.close()


def test_rebuild_is_memory_bounded(tmp_path):
    """Many GDELT rows over several days: they are read in bounded batches, only on days
    that have articles, and the clusterer never sees more than one slice at a time."""
    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    rows = []
    for day in range(3):
        t0 = NOW - timedelta(days=3 - day)
        if day != 1:  # day 1: GDELT only -> never loaded
            rows.append(_row(f"a{day}", f"Missile strike on Kyiv depot {day}", fetched=t0))
        rows += [_gdelt_row(f"{day}-{i}", fetched=t0 + timedelta(seconds=20 * i))
                 for i in range(1200)]
    lake.write("documents", rows)
    calls: list[int] = []
    real = lake.iter_query

    def spy(*a, **kw):
        for batch in real(*a, **kw):
            calls.append(len(batch))
            yield batch

    lake.iter_query = spy
    out = rebuild_events(store, lake, CFG, batch=200, step=timedelta(minutes=15))
    assert max(calls) <= 200 and sum(calls) == 2400  # day 1's 1200 rows never read
    assert out["peak_slice_docs"] <= 46  # 15 min of 20 s rows (+ the article)
    assert out["created"] == 2
    assert sum(out["gdelt_rejected"].values()) + out["gdelt_accepted"] == 2400
    store.close()


def test_live_builder_pauses_while_a_rebuild_holds_the_lock(tmp_path):
    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    lake.write("documents", [
        _row(1, "Missile strike on Kyiv power plant", fetched=NOW - timedelta(minutes=30))])
    b = EventBuilder(store, lake, Metrics(), CFG, clock=lambda: NOW)
    held, release = threading.Event(), threading.Event()

    def hold():
        with FileLock(rebuild_lock_path(store.path)):
            held.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    assert held.wait(5)
    try:
        assert b.run_once().documents == 0 and b.paused
        assert not store.list_events(limit=10)
    finally:
        release.set()
        t.join(5)
    assert b.run_once().documents == 1 and not b.paused
    # a rebuild in another process makes the live builder reload its index
    rebuild_events(store, lake, CFG)
    b.run_once()
    assert len(b.clusterer.events) == 1
    assert b.clusterer.generation == 1
    store.close()


def test_rebuild_reapplies_extraction_results(tmp_path):
    from tests.pipeline.test_event_builder import _FakeVerifier

    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    lake.write("documents", [
        _row(1, "Explosions reported in Kyiv port", fetched=NOW - timedelta(hours=2))])
    rebuild_events(store, lake, CFG)
    [ev] = store.list_events(limit=1)
    v = _FakeVerifier()
    v.recs = {ev["id"]: {"status": "done", "verdict": "mismatch", "doc_id": "doc:1",
                         "reasons": ["place: Odesa is 400 km from the event's Kyiv"],
                         "quotes": [], "result": {"where": {"place_name": "Odesa"}},
                         "geo": {"lat": 46.48, "lon": 30.72, "country": "UA",
                                 "precision": "city"}}}
    out = rebuild_events(store, lake, CFG, verifier=v, now=NOW)
    assert out["verdicts_reapplied"] == 1
    e = store.get_event(ev["id"])
    assert e["place_name"] == "Odesa" and e["lat"] == 46.48
    store.close()
