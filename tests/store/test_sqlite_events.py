from datetime import UTC, datetime, timedelta

import pytest

from osint.store.sqlite import SqliteStore, iso

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _ev(**kw):
    base = {"id": "E1", "category": "armed_conflict", "title": "t", "t_start": iso(T0),
            "t_end": iso(T0), "status": "reported", "confidence": 0.4, "version": 1,
            "mention_count": 1, "source_diversity": 1, "first_seen": iso(T0),
            "updated_at": iso(T0), "cluster_state": {"words": ["w:a"]}}
    return {**base, **kw}


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "t.sqlite")
    yield s
    s.close()


def test_upsert_keeps_first_seen_and_updates_rest(store):
    store.upsert_events([_ev()])
    later = T0 + timedelta(hours=1)
    store.upsert_events([_ev(version=2, first_seen=iso(later), updated_at=iso(later),
                             t_end=iso(later), status="multi-source")])
    e = store.get_event("E1")
    assert e["version"] == 2 and e["status"] == "multi-source"
    assert e["first_seen"] == iso(T0) and e["actors"] == [] and e["gdelt_ids"] == []
    assert store.get_event("missing") is None


def test_recent_events_returns_cluster_state(store):
    store.upsert_events([_ev(), _ev(id="E0", t_end=iso(T0 - timedelta(days=5)))])
    rows = store.recent_events(T0 - timedelta(hours=72))
    assert [r["id"] for r in rows] == ["E1"] and rows[0]["cluster_state"]["words"] == ["w:a"]


def test_documents_and_decisions_idempotent(store):
    doc = {"event_id": "E1", "doc_id": "d1", "source": "s", "url": "u", "title": "t",
           "published_at": iso(T0), "language": "en", "role": "primary",
           "added_at": iso(T0)}
    store.add_event_documents([doc, doc])
    store.add_event_fields([{"event_id": "E1", "field": "f", "value": 1, "doc_id": "d1"}] * 2)
    store.record_gdelt_decision("g1", "rejected", "stale", doc_id="dg", decided_at=T0)
    store.record_gdelt_decision("g1", "accepted", "new_event", decided_at=T0)  # first wins
    with pytest.raises(ValueError):
        store.record_gdelt_decision("g2", "maybe", "x")
    store.upsert_events([_ev()])
    e = store.get_event("E1")
    assert len(e["documents"]) == 1 and len(e["fields"]) == 1
    assert store.event_doc_ids_since(T0 - timedelta(minutes=1)) == {"d1", "dg"}
    st = store.event_stats()
    assert st["gdelt"]["rejected"]["by_reason"] == {"stale": 1}
    assert st["documents_by_role"] == {"primary": 1}
