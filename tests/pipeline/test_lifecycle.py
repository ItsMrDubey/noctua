from datetime import UTC, datetime, timedelta

import pytest

from osint.config import ROOT
from osint.pipeline.events import EventsConfig
from osint.pipeline.lifecycle import (
    LifecycleConfig,
    lifecycle_for_row,
    lifecycle_state,
    refresh_lifecycle,
)
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore, iso
from tests.pipeline.test_event_builder import NOW as BUILDER_NOW
from tests.pipeline.test_event_builder import _builder, _row

CFG = LifecycleConfig()
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(("age", "recent", "state"), [
    (timedelta(hours=6), 2, "developing"),
    (timedelta(hours=6, seconds=1), 2, "active"),
    (timedelta(hours=1), 1, "active"),  # one report only: not developing
    (timedelta(hours=48), 0, "active"),
    (timedelta(hours=48, seconds=1), 0, "cooling"),
    (timedelta(days=7), 0, "cooling"),
    (timedelta(days=7, seconds=1), 0, "closed")])
def test_boundaries(age, recent, state):
    assert lifecycle_state(NOW - age, recent, NOW, CFG) == state


def test_no_time_is_closed():
    assert lifecycle_state(None, 5, NOW, CFG) == "closed"


def test_thresholds_come_from_events_yaml():
    cfg = EventsConfig.from_file(ROOT / "config" / "events.yaml")
    assert cfg.lifecycle == LifecycleConfig(developing_h=6.0, developing_min_docs=2,
                                            active_h=48.0, cooling_d=7.0)
    assert EventsConfig.from_dict({}).lifecycle == LifecycleConfig()


def test_sensor_rows_use_t_end_and_others_the_newest_article():
    sensor = {"origin": "sensor", "t_end": iso(NOW - timedelta(hours=1)),
              "last_doc_at": None, "recent_docs": 0}
    assert lifecycle_for_row(sensor, NOW, CFG) == "active"
    news = {"origin": "news", "t_end": iso(NOW - timedelta(hours=1)),
            "last_doc_at": iso(NOW - timedelta(days=3)), "recent_docs": 0}
    assert lifecycle_for_row(news, NOW, CFG) == "cooling"
    bare = {"origin": "news", "t_end": iso(NOW - timedelta(days=9)), "last_doc_at": None,
            "recent_docs": 0}
    assert lifecycle_for_row(bare, NOW, CFG) == "closed"


def _ev(eid, t_end, **kw):
    base = {"id": eid, "category": "armed_conflict", "title": "t", "t_start": iso(t_end),
            "t_end": iso(t_end), "status": "reported", "confidence": 0.4, "version": 1,
            "mention_count": 1, "source_diversity": 1, "first_seen": iso(t_end),
            "updated_at": iso(t_end)}
    return {**base, **kw}


def _doc(eid, did, at):
    return {"event_id": eid, "doc_id": did, "source": "rss:t", "url": f"https://{did}.x/a",
            "title": "t", "published_at": iso(at), "language": "en", "role": "mention",
            "added_at": iso(at)}


def test_refresh_lifecycle_writes_changes_and_reopens_touched_events(tmp_path):
    store = SqliteStore(tmp_path / "s.sqlite")
    store.save_events(
        [_ev("DEV", NOW), _ev("OLD", NOW - timedelta(days=10)),
         _ev("SEN", NOW - timedelta(hours=1), origin="sensor"),
         _ev("SHUT", NOW - timedelta(days=20), lifecycle="closed")],
        documents=[_doc("DEV", "a", NOW - timedelta(hours=1)),
                   _doc("DEV", "b", NOW - timedelta(hours=2)),
                   _doc("OLD", "c", NOW - timedelta(days=10)),
                   _doc("SHUT", "d", NOW - timedelta(days=20))])
    assert refresh_lifecycle(store, NOW, CFG) == 3
    got = {e: store.get_event(e)["lifecycle"] for e in ("DEV", "OLD", "SEN", "SHUT")}
    assert got == {"DEV": "developing", "OLD": "closed", "SEN": "active", "SHUT": "closed"}
    assert refresh_lifecycle(store, NOW, CFG) == 0  # nothing changed
    store.add_event_documents([_doc("SHUT", "e", NOW - timedelta(minutes=5))])
    assert refresh_lifecycle(store, NOW, CFG) == 0  # closed events are not rescanned...
    assert refresh_lifecycle(store, NOW, CFG, ["SHUT"]) == 1  # ...unless a pass touched them
    assert store.get_event("SHUT")["lifecycle"] == "active"
    assert refresh_lifecycle(store, NOW, CFG, []) == 0
    store.close()


def test_open_events_query_uses_partial_index(tmp_path):
    store = SqliteStore(tmp_path / "s.sqlite")
    plan = " ".join(str(tuple(r)) for r in store._db.execute(
        "EXPLAIN QUERY PLAN SELECT e.id FROM events e "
        "WHERE e.lifecycle IS NULL OR e.lifecycle != 'closed'"))
    assert "events_open" in plan
    store.close()


def test_builder_pass_sets_lifecycle(tmp_path):
    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    lake.write("documents", [
        _row(1, "Missile strike on Kyiv power plant",
             fetched=BUILDER_NOW - timedelta(minutes=50)),
        _row(2, "Missile strike on Kyiv power plant",
             fetched=BUILDER_NOW - timedelta(minutes=40), host="other.example")])
    b = _builder(store, lake, lambda: BUILDER_NOW)
    s = b.run_once()
    [ev] = store.list_events(limit=5)
    assert ev["lifecycle"] == "developing" and s.lifecycle_changed >= 1
    store.close()
