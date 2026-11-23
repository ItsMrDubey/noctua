"""event_status_history: recorded by triggers on every writer, baseline for old rows."""
import sqlite3

from osint.store.sqlite import SqliteStore


def _ev(eid, status="reported", **kw):
    return {"id": eid, "category": "armed_conflict", "title": "t", "summary": "", "lat": 1.0,
            "lon": 2.0, "precision": "city", "t_start": "2026-11-23T01:00:00.000+00:00",
            "t_end": "2026-11-23T01:00:00.000+00:00", "status": status, "confidence": 0.5,
            "version": 2, "mention_count": 1, "source_diversity": 1,
            "first_seen": "2026-11-23T01:00:00.000+00:00",
            "updated_at": "2026-11-23T01:00:00.000+00:00", **kw}


def test_insert_change_and_noop_updates(tmp_path):
    s = SqliteStore(tmp_path / "s.sqlite")
    s.save_events([_ev("E1")])
    s.save_events([_ev("E1", confidence=0.6)])  # same status: nothing recorded
    s.save_events([_ev("E1", status="multi-source")])
    s.save_corroborations([], events=[_ev("E1", status="sensor-corroborated")])
    h = s.status_history("E1")
    assert [(r["prev_status"], r["status"], r["source"]) for r in h] == [
        (None, "reported", "insert"), ("reported", "multi-source", "change"),
        ("multi-source", "sensor-corroborated", "change")]
    assert all(r["at"].endswith("+00:00") and len(r["at"]) == 29 for r in h)
    s.delete_events(["E1"])
    assert s.status_history("E1") == []
    s.save_events([_ev("ucdp-1", status="historical", origin="historical")])
    assert s.status_history("ucdp-1") == []
    s.close()


def test_baseline_for_events_that_predate_history(tmp_path):
    path = tmp_path / "old.sqlite"
    s = SqliteStore(path)
    s.save_events([_ev("OLD", status="multi-source")])
    s.close()
    db = sqlite3.connect(path)
    db.execute("DELETE FROM event_status_history")  # as if written before the triggers existed
    db.commit()
    db.close()
    s = SqliteStore(path, migrate=False)  # the API process never writes a baseline
    assert s.status_history("OLD") == []
    s.close()
    s = SqliteStore(path)
    assert [(r["status"], r["source"]) for r in s.status_history("OLD")] == [("multi-source",
                                                                              "baseline")]
    s.close()
    s = SqliteStore(path)  # idempotent
    assert len(s.status_history("OLD")) == 1
    s.close()
