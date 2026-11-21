"""Historical (UCDP) and sensor events survive every EventBuilder start and maintenance."""

from datetime import UTC, datetime, timedelta

from osint.config import ROOT
from osint.core.metrics import Metrics
from osint.pipeline.events import (
    REBUILD_STATE,
    EventBuilder,
    EventsConfig,
    migrate_events,
)
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore, iso

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _ucdp(i: int, t: datetime) -> dict:
    return {"id": f"ucdp-{i}", "category": "armed_conflict", "title": f"u{i}",
            "t_start": iso(t), "t_end": iso(t), "status": "historical", "confidence": 0.8,
            "version": 1, "mention_count": 1, "source_diversity": 1, "first_seen": iso(t),
            "updated_at": iso(t), "lat": 1.0, "lon": 2.0, "precision": "point",
            "cluster_state": {"source": "ucdp", "ucdp_id": str(i)}}


def _seed(store: SqliteStore) -> None:
    t = NOW - timedelta(hours=1)  # inside the clusterer's warm-start window
    store.save_events([_ucdp(1, t), _ucdp(2, NOW - timedelta(days=400))], documents=[{
        "event_id": "ucdp-1", "doc_id": "ucdp:1", "source": "ucdp", "url": "u", "title": "u1",
        "published_at": iso(t), "role": "primary", "added_at": iso(t)}])
    store.save_sensor_event(
        {**_ucdp(9, t), "id": "S1", "status": "sensor-corroborated",
         "cluster_state": {"sensor": True}},
        {"detector": "orbit", "dedupe_key": "orbit:x", "cell": "c"}, [],
        {"event_id": "S1", "score": 1.0, "kinds": [], "evidence": 0, "corroborated": True})


def _ids(store: SqliteStore) -> set[str]:
    return {e["id"] for e in store.list_events(limit=100)}


def test_historical_and_sensor_events_survive_restarts_after_a_rebuild(tmp_path):
    store = SqliteStore(tmp_path / "s.sqlite")
    lake = Lake(tmp_path / "lake")
    _seed(store)
    # a rebuild marker from an earlier run (old format: no "migrated" flag)
    store.set_cursor(REBUILD_STATE, {"fetched_at_us": 1, "fetched_at": iso(NOW)})
    cfg = EventsConfig.from_file(ROOT / "config" / "events.yaml")
    for _ in range(3):  # three simulated server starts
        b = EventBuilder(store, lake, Metrics(), cfg, clock=lambda: NOW)
        b.warm_start()
        assert not set(b.clusterer.events) & {"ucdp-1", "S1"}  # never clustered against
        assert _ids(store) == {"ucdp-1", "ucdp-2", "S1"}
    assert store.get_cursor(REBUILD_STATE)["migrated"] is True
    store.close()


def test_migration_runs_once_per_rebuild_marker(tmp_path):
    store = SqliteStore(tmp_path / "s.sqlite")
    store.set_cursor(REBUILD_STATE, {"at": "x"})
    legacy = {**_ucdp(5, NOW), "id": "OLD", "status": "reported", "cluster_state": {}}
    store.save_events([legacy], documents=[{
        "event_id": "OLD", "doc_id": "a", "source": "s", "url": "u", "title": "t",
        "published_at": iso(NOW), "role": "primary", "added_at": iso(NOW)}])
    assert migrate_events(store)["legacy"] == 1
    store.save_events([legacy], documents=[{
        "event_id": "OLD", "doc_id": "a", "source": "s", "url": "u", "title": "t",
        "published_at": iso(NOW), "role": "primary", "added_at": iso(NOW)}])
    assert migrate_events(store)["legacy"] == 0  # marker already migrated
    store.close()
