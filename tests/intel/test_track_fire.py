"""Contextual subjects of the intel console: the 24 h track of an aircraft / ship
(osint.intel.context.section_track) and a fire's conflict score (section_fire)."""
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from osint.core.models import Position, TrackInfo
from osint.intel import aggregate, context
from osint.store.sqlite import SqliteStore, iso

NOW = datetime.now(UTC).replace(microsecond=0)
us = lambda dt: int(dt.timestamp() * 1e6)


class TrackLake:
    """positions of one ship: under way, a 4 h AIS gap, then 2 h stopped on a pipeline."""

    def __init__(self, rows):
        self.rows = rows

    def query(self, sql, params=None, start=None, end=None):
        if "WHERE track_id = ?" in sql:
            return self.rows if params[0] == "sea:273" else []
        if "type = 'fire'" in sql:
            return [(us(NOW - timedelta(hours=2)), 321.0, json.dumps({"satellite": "N21",
                                                                     "confidence": "h",
                                                                     "daynight": "N"}))]
        return []


def _ship_rows():
    rows, t = [], NOW - timedelta(hours=20)
    for i in range(12):  # under way eastwards at 10 kn
        rows.append((us(t), 44.0, 33.0 + i * 0.03, None, 10.0))
        t += timedelta(minutes=10)
    t += timedelta(hours=4)  # dark period
    for _ in range(13):  # 2 h stopped at the pipeline
        rows.append((us(t), 44.0, 33.6, None, 0.2))
        t += timedelta(minutes=10)
    rows.append((us(t), 44.0, 33.7, None, 9.0))
    return rows


@pytest.fixture
def rt(tmp_path, monkeypatch):
    from osint.infra import loader

    store = SqliteStore(tmp_path / "osint.sqlite")
    store.save_events([{"id": "01EVNEARPATH", "category": "maritime_security", "title": "Tanker",
                        "summary": "", "actors": [], "lat": 44.05, "lon": 33.3, "precision": "point",
                        "place_name": "Black Sea", "country": None, "geo_confidence": 0.9,
                        "t_start": iso(NOW - timedelta(hours=3)), "t_end": iso(NOW - timedelta(hours=3)),
                        "status": "reported", "confidence": 0.6, "version": 1, "mention_count": 1,
                        "source_diversity": 1, "first_seen": iso(NOW), "updated_at": iso(NOW)}])
    store.upsert_tracks([TrackInfo(track_id="sea:273", kind="sea", ident="273", name="SHADOW ONE",
                                   category="Tanker",
                                   extra={"destination": "NOVOROSSIYSK", "imo": "9000001"})], NOW)
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "pipelines_osm.json").write_text(json.dumps([
        {"id": "p1", "name": "Blue Stream", "substance": "gas", "offshore": True,
         "line": [[33.6, 43.0], [33.6, 45.0]]}]))
    (ref / "cables_tg.json").write_text(json.dumps({"cables": [], "landings": []}))
    monkeypatch.setattr(loader, "get_infra", lambda: loader.Infra(ref))
    ingestor = SimpleNamespace(live={"sea:273": Position(track_id="sea:273", kind="sea", ts=NOW,
                                                          lat=44.0, lon=33.7, source="ais",
                                                          speed_kts=9.0, heading_deg=90.0)},
                               _lock=threading.Lock())
    r = SimpleNamespace(store=store, lake=TrackLake(_ship_rows()), ingestor=ingestor,
                        settings=SimpleNamespace(data_dir=tmp_path, config_dir=tmp_path, api_token=""))
    yield r
    store.close()


def test_ship_track_gaps_loiter_lines_events(rt):
    subj = {"kind": "ship", "id": "sea:273", "lat": 44.0, "lon": 33.7, "t": iso(NOW), "radius_km": 50.0}
    T = context.section_track(rt, subj, NOW)
    assert T["kind"] == "ship" and T["live"]["speed_kts"] == 9.0 and T["fixes"] == 26
    assert T["meta"]["name"] == "SHADOW ONE" and T["extra"]["destination"] == "NOVOROSSIYSK"
    assert T["gaps"][0]["minutes"] == 250 and T["gaps"][0]["jump_km"] > 0
    lo = T["loiters"][0]
    assert lo["minutes"] == 120 and lo["port"] is None and lo["near_lines"][0]["name"] == "Blue Stream"
    assert T["lines_crossed"][0]["name"] == "Blue Stream"
    assert [e["id"] for e in T["events_near"]] == ["01EVNEARPATH"]
    texts = [a["text"] for a in T["alerts"]]
    assert any(t.startswith("AIS gap 4 h 10 min") for t in texts)
    assert any("Loitered 120 min" in t and "Blue Stream" in t for t in texts)
    assert len(T["path"]) == 26 and T["summary"]["distance_km"] > 30


def test_track_watchlist_and_emergency(rt, tmp_path):
    db = sqlite3.connect(tmp_path / "infra_watch.sqlite")
    db.executescript("CREATE TABLE active (track_id TEXT PRIMARY KEY, severity TEXT, name TEXT, lat REAL,"
                     " lon REAL, ts TEXT, hits TEXT); CREATE TABLE alerts (id INTEGER PRIMARY KEY, ts TEXT,"
                     " kind TEXT, track_id TEXT, severity TEXT, title TEXT, sub TEXT, lat REAL, lon REAL,"
                     " detail TEXT);")
    db.execute("INSERT INTO active VALUES ('sea:273','high','x',44,33,?,?)", (iso(NOW), json.dumps(
        [{"entry": {"risk": ["Shadow fleet"], "source": "opensanctions"}, "matched_on": "imo",
          "strength": "strong"}])))
    db.commit()
    db.close()
    subj = {"kind": "ship", "id": "sea:273", "lat": 44.0, "lon": 33.7, "t": iso(NOW), "radius_km": 50.0}
    T = context.section_track(rt, subj, NOW)
    assert T["alerts"][0] == {"level": "alert", "text": "Watchlist: Shadow fleet",
                              "basis": "opensanctions · matched on imo (strong)"}
    assert context.section_track(rt, {**subj, "kind": "event"}, NOW) is None


def test_fire_score_section(rt):
    from osint.fires.store import get_score_store

    get_score_store(rt).save([{"id": "fire:x", "ts": NOW - timedelta(hours=2), "lat": 48.1, "lon": 37.7,
                               "score": 88, "reasons": ["near_frontline", "night"],
                               "features": {"frontline_km": 4.2, "points": {"near_frontline": 40.0}}}],
                             fp="t")
    F = context.section_fire(rt, {"kind": "fire", "id": "fire:x", "t": iso(NOW - timedelta(hours=2))}, NOW)
    assert F["score"] == 88 and F["war_likely"] and F["threshold"] == 50
    assert F["reasons"] == ["near_frontline", "night"] and F["features"]["frontline_km"] == 4.2
    assert F["detection"]["frp_mw"] == 321.0 and F["detection"]["satellite"] == "N21"
    assert context.section_fire(rt, {"kind": "event", "id": "x"}, NOW) is None


def test_kind_names_normalised_in_traffic():
    assert context.KIND_NAME["air"] == "aircraft" and context.KIND_NAME["sea"] == "ship"
    assert "track" in aggregate.SECTIONS and "fire" in aggregate.SECTIONS
