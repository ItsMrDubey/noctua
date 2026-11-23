import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from osint.config import ROOT
from osint.core.metrics import Metrics
from osint.core.models import Document, PlaceHit, TriageResult
from osint.pipeline.events import CURSOR_NAME, EventBuilder, EventsConfig, load_cameo_map
from osint.store.lake import Lake, document_row
from osint.store.sqlite import SqliteStore

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
KYIV = PlaceHit(name="Kyiv", country="UA", lat=50.45, lon=30.52, precision="city",
                confidence=0.9, matched="Kyiv")


def _row(i, title, *, fetched, host=None, relevance=0.8, kind="article", attrs=None,
         place=KYIV, cats=("armed_conflict",)):
    d = Document(id=f"doc:{i}", source="rss:t", url=f"https://{host or f'o{i}.example'}/{i}",
                 title=title, snippet="", language="en", published_at=fetched,
                 fetched_at=fetched, kind=kind, origin_id=str(i), attrs=attrs or {})
    t = TriageResult(language="en", relevance=relevance, categories=list(cats),
                     places=[place] if place else [], best_place=place)
    return document_row(d, t)


def _builder(store, lake, clock, metrics=None):
    return EventBuilder(store, lake, metrics or Metrics(), EventsConfig.from_file(
        ROOT / "config" / "events.yaml"), clock=clock)


def test_config_file_loads():
    c = EventsConfig.from_file(ROOT / "config" / "events.yaml")
    assert c.window_h("armed_conflict") == 12 and c.window_h("political") == 48
    assert c.min_shared("political") == 2 and c.min_shared("armed_conflict") == 1
    assert "19" in c.gdelt_tracked_roots and "04" not in c.gdelt_tracked_roots
    assert abs(sum(c.weights.values()) - 1.0) < 1e-9
    assert EventsConfig.from_file(ROOT / "nope.yaml").interval_s == 60


def test_cameo_map_from_triage_yaml():
    m = load_cameo_map(ROOT / "config" / "triage.yaml")
    assert m("190", "19") == "armed_conflict" and m("1384", "13") == "armed_conflict"
    assert m("999", "99") is None


def test_builder_pass_persists_and_cursor_resumes(tmp_path):
    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    now = [NOW]
    m = Metrics()
    lake.write("documents", [
        _row(1, "Missile strike on Kyiv power plant", fetched=NOW - timedelta(minutes=50)),
        _row(2, "Missile strike on Kyiv power plant", fetched=NOW - timedelta(minutes=40),
             host="other.example"),
        _row(3, "Celebrity gossip", fetched=NOW - timedelta(minutes=30), relevance=0.1),
        _row(4, "RUSSIA — Fight — UKRAINE", kind="gdelt_event", fetched=NOW - timedelta(
            minutes=30), attrs={"event_root_code": "04", "event_code": "040"}),
    ])
    b = _builder(store, lake, lambda: now[0], m)
    s1 = b.run_once()
    assert s1.documents == 3  # irrelevant article filtered in SQL, GDELT always read
    assert s1.created == 1 and s1.clustered == 2
    assert s1.gdelt_rejected == {"irrelevant_category": 1}
    assert m.get("events_created_total") == 1
    assert m.get("gdelt_rejected_total", reason="irrelevant_category") == 1
    assert m.get("documents_clustered_total") == 2
    cur = store.get_cursor(CURSOR_NAME)
    assert cur["fetched_at"].startswith("2026-11-21T11:30")
    [ev] = store.list_events(limit=10)
    assert ev["status"] == "multi-source" and ev["mention_count"] == 2

    # a second pass with nothing new re-reads the overlap window but changes nothing
    assert b.run_once().documents == 0

    # restart: a fresh builder resumes from the cursor and the persisted index
    now[0] = NOW + timedelta(minutes=5)
    lake.write("documents", [
        _row(5, "Kyiv power plant hit by missiles, officials say", host="third.example",
             fetched=NOW + timedelta(minutes=2))])
    b2 = _builder(store, lake, lambda: now[0])
    s2 = b2.run_once()
    assert s2.documents == 1 and s2.updated == 1 and s2.created == 0
    full = store.get_event(ev["id"])
    assert full["mention_count"] == 3 and full["source_diversity"] == 3
    assert len(full["documents"]) == 3
    stats = store.event_stats()
    assert stats["gdelt"]["rejected"]["by_reason"] == {"irrelevant_category": 1}
    store.close()


async def test_run_loop_and_stop(tmp_path):
    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    lake.write("documents", [_row(1, "Missile strike on Kyiv", fetched=NOW)])
    b = _builder(store, lake, lambda: NOW + timedelta(minutes=1))
    b.cfg.interval_s = 0.01
    task = asyncio.create_task(b.run())
    for _ in range(200):
        if b.passes >= 2:
            break
        await asyncio.sleep(0.01)
    b.stop()
    await asyncio.wait_for(task, 2)
    assert b.passes >= 2 and b.last_error is None
    assert len(store.list_events()) == 1
    store.close()


def test_wire_attaches_builder(tmp_path):
    from osint.config import Settings
    from osint.pipeline.events import wire
    from osint.runtime import Runtime

    rt = Runtime(Settings(_env_file=None, data_dir=tmp_path), [])
    b = wire(rt)
    assert rt.event_builder is b and b.store is rt.store
    rt.store.close()


def _gdelt_row(i, *, fetched, lat=50.45, lon=30.52):
    attrs = {"event_root_code": "19", "event_code": "190", "num_mentions": 10,
             "geo_type": 4, "geo_lat": lat, "geo_lon": lon, "geo_name": "Kyiv",
             "actor1": "RUSSIA", "actor2": "UKRAINE", "sqldate": fetched.strftime("%Y%m%d")}
    place = PlaceHit(name="Kyiv", country="UP", lat=lat, lon=lon, precision="city",
                     confidence=0.7, matched="gdelt:ActionGeo")
    return _row(f"g{i}", "RUSSIA — Fight — UKRAINE", kind="gdelt_event", fetched=fetched,
                attrs=attrs, place=place, relevance=0.6)


def test_migration_drops_gdelt_only_events_idempotently(tmp_path):
    from osint.pipeline.events import migrate_events
    from osint.store.sqlite import iso

    store = SqliteStore(tmp_path / "s.sqlite")
    t = iso(NOW)
    base = {"category": "armed_conflict", "title": "x", "t_start": t, "t_end": t,
            "status": "reported", "confidence": 0.5, "version": 1, "mention_count": 1,
            "source_diversity": 1, "first_seen": t, "updated_at": t,
            "cluster_state": {"v": 2}}
    doc = {"source": "s", "url": "u", "title": "x", "published_at": t, "added_at": t}
    store.save_events(
        [{"id": "EG", **base}, {"id": "EA", **base}],
        documents=[{"event_id": "EG", "doc_id": "g1", "role": "gdelt", **doc},
                   {"event_id": "EA", "doc_id": "a1", "role": "primary", **doc},
                   {"event_id": "EA", "doc_id": "g2", "role": "gdelt", **doc}],
        decisions=[{"gdelt_event_id": "1", "decision": "accepted", "reason": "new_event",
                    "event_id": "EG", "doc_id": "g1", "decided_at": t},
                   {"gdelt_event_id": "2", "decision": "accepted",
                    "reason": "duplicate_of_event", "event_id": "EA", "doc_id": "g2",
                    "decided_at": t}])
    out = migrate_events(store)
    assert out == {"gdelt_only": 1, "legacy": 0, "decisions": 1}
    assert [e["id"] for e in store.list_events(limit=10)] == ["EA"]
    assert store.get_event("EG") is None
    g = store.event_stats()["gdelt"]
    assert g["rejected"]["by_reason"] == {"no_article_backing": 1}
    assert g["accepted"]["by_reason"] == {"duplicate_of_event": 1}
    assert migrate_events(store) == {"gdelt_only": 0, "legacy": 0, "decisions": 0}
    store.close()


def test_rebuild_reclusters_lake_and_drops_legacy_events(tmp_path):
    from osint.pipeline.events import REBUILD_STATE, migrate_events, rebuild_events

    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    lake.write("documents", [
        _row(1, "Missile strike on Kyiv power plant", fetched=NOW - timedelta(hours=5)),
        _row(2, "Missile strike on Kyiv power plant", host="b.example",
             fetched=NOW - timedelta(hours=4, minutes=50)),
        _gdelt_row(1, fetched=NOW - timedelta(hours=4, minutes=40)),
        _gdelt_row(2, fetched=NOW - timedelta(hours=1), lat=10.0, lon=10.0),  # no event there
    ])
    cfg = EventsConfig.from_file(ROOT / "config" / "events.yaml")
    out = rebuild_events(store, lake, cfg)
    assert out["created"] == 1 and out["gdelt_accepted"] == 1
    assert out["gdelt_rejected"] == {"no_article_backing": 1}
    [ev] = store.list_events(limit=10)
    assert ev["title"] == "Missile strike on Kyiv power plant" and ev["status"] == "multi-source"
    assert store.get_cursor(REBUILD_STATE)["fetched_at"].startswith("2026-11-21T11:00")
    # a second rebuild gives the same result (no duplicates)
    rebuild_events(store, lake, cfg)
    [ev] = store.list_events(limit=10)
    # an event written by old code after the rebuild is dropped at the next start
    legacy = dict(store.list_events(limit=1, slim=False)[0])
    legacy.update(id="OLD", cluster_state={})
    store.save_events([legacy], documents=[{
        "event_id": "OLD", "doc_id": "doc:1", "source": "s", "url": "u", "title": "t",
        "published_at": legacy["t_start"], "role": "primary", "added_at": legacy["t_start"]}])
    assert migrate_events(store)["legacy"] == 1
    assert {e["id"] for e in store.list_events(limit=10)} == {ev["id"]}
    store.close()


class _FakeVerifier:
    def __init__(self):
        self.recs = {}

    def verdicts_since(self, since):
        return {k: v["verdict"] for k, v in self.recs.items()}

    def get_many(self, ids):
        return {i: self.recs[i] for i in ids if i in self.recs}


def test_extraction_verdicts_merge_into_events(tmp_path):
    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    lviv = PlaceHit(name="Lviv", country="UA", lat=49.84, lon=24.03, precision="city",
                    confidence=0.9, matched="Lviv")
    odesa = PlaceHit(name="Odesa", country="UA", lat=46.48, lon=30.72, precision="city",
                     confidence=0.9, matched="Odesa")
    lake.write("documents", [
        _row(1, "Missile strike on Kyiv power plant", fetched=NOW - timedelta(minutes=50)),
        _row(2, "Drone strike hits Lviv depot", fetched=NOW - timedelta(minutes=40),
             place=lviv),
        _row(3, "Explosions reported in Odesa port", fetched=NOW - timedelta(minutes=30),
             place=odesa)])
    m = Metrics()
    b = _builder(store, lake, lambda: NOW, m)
    b.run_once()
    ids = {e["title"].split()[0]: e["id"] for e in store.list_events(limit=10)}
    before = {k: store.get_event(v)["confidence"] for k, v in ids.items()}
    v = _FakeVerifier()
    v.recs = {
        ids["Missile"]: {"status": "done", "verdict": "supported", "reasons": [],
                         "doc_id": "doc:1", "geo": None,
                         "quotes": [{"field": "casualties", "quote": "two killed",
                                     "verbatim": True},
                                    {"field": "where", "quote": "paraphrase",
                                     "verbatim": False}]},
        ids["Drone"]: {"status": "done", "verdict": "unsupported",
                       "reasons": ["article does not report a concrete event"],
                       "doc_id": "doc:2", "geo": None, "quotes": []},
        ids["Explosions"]: {"status": "done", "verdict": "mismatch",
                            "reasons": ["place: Mykolaiv is 110 km from the event's Odesa"],
                            "doc_id": "doc:3", "quotes": [],
                            "geo": {"lat": 46.97, "lon": 31.99, "country": "UA",
                                    "precision": "city"},
                            "result": {"where": {"place_name": "Mykolaiv"}}},
    }
    b.verifier = v
    s = b.run_once()
    assert s.verdicts_applied == 3
    sup, uns, mis = (store.get_event(ids[k]) for k in ("Missile", "Drone", "Explosions"))
    assert sup["confidence"] == pytest.approx(min(1, before["Missile"] + 0.1), abs=0.02)
    assert {(f["field"], f["value"]) for f in sup["fields"]} >= {("casualties", "two killed")}
    assert not any(f["value"] == "paraphrase" for f in sup["fields"])
    assert uns["confidence"] <= 0.2
    assert mis["place_name"] == "Mykolaiv" and mis["lat"] == 46.97
    assert mis["confidence"] < before["Explosions"]
    assert any(f["field"] == "location_before_extraction" and "Odesa" in f["value"]
               for f in mis["fields"])
    shown = {e["id"] for e in store.list_events(map_default=True, limit=10)}
    assert ids["Drone"] not in shown
    assert m.get("event_verdicts_applied_total", verdict="mismatch") == 1
    # unchanged verdicts are not re-applied; a restart keeps them in the scorer
    assert b.run_once().verdicts_applied == 0
    b2 = _builder(store, lake, lambda: NOW)
    b2.warm_start()
    assert b2.verdicts[ids["Drone"]] == "unsupported"
    store.close()


def test_cursor_advances_past_overlap_window_filling_a_page(tmp_path):
    # a bulk write puts more documents in the overlap window than one page holds: the
    # builder must page from the cursor instead of re-reading the same page forever
    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    now = [NOW]
    bulk = NOW - timedelta(minutes=30)
    lake.write("documents", [
        _row(i, f"Strike report {i}", fetched=bulk + timedelta(seconds=i)) for i in range(6)])
    b = _builder(store, lake, lambda: now[0])
    b.cfg.batch_limit = 4
    b.run_once()
    b.run_once()
    lake.write("documents", [
        _row(100, "Missile strike on Kyiv power plant", fetched=NOW - timedelta(minutes=1))])
    for _ in range(3):
        b.run_once()
    assert store.get_cursor(CURSOR_NAME)["fetched_at"].startswith("2026-11-21T11:59")
    store.close()


def test_backlog_flag_set_while_pages_are_full(tmp_path):
    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    lake.write("documents", [
        _row(i, f"Strike report {i}", fetched=NOW - timedelta(minutes=30 - i)) for i in range(5)])
    b = _builder(store, lake, lambda: NOW)
    b.cfg.batch_limit = 3
    b.run_once()
    assert b.backlog
    for _ in range(4):  # catch-up passes until a page is no longer full
        b.run_once()
        if not b.backlog:
            break
    assert not b.backlog
    assert store.get_cursor(CURSOR_NAME)["fetched_at"].startswith("2026-11-21T11:34")
    store.close()
