import asyncio
from datetime import UTC, datetime, timedelta

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
