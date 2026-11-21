"""Event origin (news | sensor | historical): maintenance paths only ever touch news."""

import sqlite3
from datetime import UTC, datetime, timedelta

from osint.store.sqlite import SqliteStore, iso

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _ev(eid, **kw):
    base = {"id": eid, "category": "armed_conflict", "title": "t", "t_start": iso(T0),
            "t_end": iso(T0), "status": "reported", "confidence": 0.4, "version": 1,
            "mention_count": 1, "source_diversity": 1, "first_seen": iso(T0),
            "updated_at": iso(T0), "cluster_state": {"v": 2}}
    return {**base, **kw}


def _ucdp(eid="ucdp-1", **kw):
    return _ev(eid, status="historical", cluster_state={"source": "ucdp"}, **kw)


def _doc(eid, doc_id, role="primary"):
    t = iso(T0)
    return {"event_id": eid, "doc_id": doc_id, "source": "s", "url": "u", "title": "x",
            "published_at": t, "role": role, "added_at": t}


def _sensor(store, eid="S1"):
    store.save_sensor_event(_ev(eid, status="sensor-corroborated", cluster_state={}),
                            {"detector": "orbit", "dedupe_key": f"orbit:{eid}", "cell": "c"},
                            [], {"event_id": eid, "score": 1.0, "kinds": [], "evidence": 0,
                                 "corroborated": True})


def _origins(store):
    with store._lock:
        return dict(store._db.execute("SELECT id, origin FROM events").fetchall())


def test_origin_is_derived_on_write(tmp_path):
    store = SqliteStore(tmp_path / "s.sqlite")
    store.save_events([_ev("N1"), _ucdp()], [_doc("N1", "a1"), _doc("ucdp-1", "ucdp:1")])
    _sensor(store)
    assert _origins(store) == {"N1": "news", "ucdp-1": "historical", "S1": "sensor"}
    # an upsert without origin never turns a sensor/historical row into news
    store.save_events([_ev("S1", status="sensor-corroborated")])
    assert _origins(store)["S1"] == "sensor"
    store.close()


def test_migration_backfills_origin_of_existing_rows(tmp_path):
    path = tmp_path / "old.sqlite"
    store = SqliteStore(path)
    store.save_events([_ev("N1"), _ucdp(), _ev("U2", cluster_state={"source": "ucdp"})])
    _sensor(store)
    store.close()
    db = sqlite3.connect(path)  # simulate a pre-origin database
    db.execute("DROP INDEX events_origin")
    db.execute("ALTER TABLE events DROP COLUMN origin")
    db.commit()
    db.close()
    store = SqliteStore(path)
    assert _origins(store) == {"N1": "news", "ucdp-1": "historical", "U2": "historical",
                               "S1": "sensor"}
    store.close()


def test_maintenance_never_deletes_historical_or_sensor(tmp_path):
    store = SqliteStore(tmp_path / "s.sqlite")
    # no article document at all: historical/sensor must still not be "gdelt only"
    store.save_events([_ev("N1"), _ucdp(), _ev("NG")],
                      [_doc("N1", "a1"), _doc("NG", "g1", role="gdelt"),
                       _doc("ucdp-1", "ucdp:1")])
    _sensor(store)
    assert store.gdelt_only_event_ids() == ["NG"]
    assert sorted(store.legacy_event_ids(3)) == ["N1", "NG"]  # ucdp has no v: not legacy
    assert store.delete_events(["ucdp-1", "S1", "NG"]) == 1  # news only by default
    assert set(_origins(store)) == {"N1", "ucdp-1", "S1"}
    assert store.clear_news_events() == 1
    assert set(_origins(store)) == {"ucdp-1", "S1"}
    assert store.get_event("ucdp-1")["documents"]  # its document survived
    store.close()


def test_recent_events_is_news_only(tmp_path):
    store = SqliteStore(tmp_path / "s.sqlite")
    store.save_events([_ev("N1"), _ucdp()])
    _sensor(store)
    assert [r["id"] for r in store.recent_events(T0 - timedelta(hours=1))] == ["N1"]
    assert {e["id"] for e in store.list_events(origin="news,sensor")} == {"N1", "S1"}
    store.close()


def test_connection_settings_and_checkpoint(tmp_path):
    store = SqliteStore(tmp_path / "s.sqlite")
    with store._lock:
        assert store._db.execute("PRAGMA journal_size_limit").fetchone()[0] == 67108864
        assert store._db.execute("PRAGMA busy_timeout").fetchone()[0] >= 30000
    store.save_events([_ev("N1")])
    assert store.checkpoint()["busy"] == 0
    store.close()
