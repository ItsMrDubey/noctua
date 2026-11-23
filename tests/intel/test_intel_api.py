"""GET /api/intel, /api/events/{id}/timeline and /export against a real SqliteStore; the lake
and live tracks are small fakes (their SQL is exercised against the real lake elsewhere)."""
import threading
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.intel import mount_intel
from osint.intel import aggregate, hooks
from osint.intel.aggregate import repost_groups
from osint.store.sqlite import SqliteStore, iso

NOW = datetime.now(UTC).replace(microsecond=0)
T0 = NOW - timedelta(hours=4)
EID = "01EVINTEL000000000000000001"


def _ev(eid, lat, lon, *, status="multi-source", category="armed_conflict", title="Refinery fire",
        t=T0, place="Novoshakhtinsk", **kw):
    return {"id": eid, "category": category, "title": title, "summary": "s",
            "actors": [{"name": "Ukraine", "role": "source", "type": "UKR"}], "lat": lat, "lon": lon,
            "precision": "city", "place_name": place, "country": "RU", "geo_confidence": 0.9,
            "t_start": iso(t), "t_end": iso(t), "status": status, "confidence": 0.8, "version": 2,
            "mention_count": 2, "source_diversity": 2, "first_seen": iso(t),
            "updated_at": iso(t), **kw}


class FakeLake:
    def __init__(self):
        self.sql = []

    def query(self, sql, params=None, start=None, end=None):
        self.sql.append(sql)
        if "FROM observations" in sql and params and params[0] == "fire":
            us = int((T0 - timedelta(minutes=70) - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds() * 1e6)
            return [("fire:a", us, 47.83, 39.86, 321.0, '{"satellite": "N21", "confidence": "n"}')]
        return []


@pytest.fixture
def rt(tmp_path):
    store = SqliteStore(tmp_path / "osint.sqlite")
    store.save_events(
        [_ev(EID, 47.76, 39.93),
         _ev("01EVNEAR", 47.80, 39.90, title="Second report", status="reported",
             t=T0 - timedelta(days=2)),
         _ev("01EVFAR", 10.0, 10.0, title="Elsewhere"),
         _ev("ucdp-1", 47.9, 39.95, status="historical", origin="historical",
             title="Old clash", t=datetime(2024, 5, 1, tzinfo=UTC))],
        documents=[{"event_id": EID, "doc_id": "d1", "source": "rss:ki", "url": "https://k.example/a",
                    "title": "Refinery reportedly burning", "published_at": iso(T0),
                    "language": "en", "role": "primary", "added_at": iso(T0)},
                   {"event_id": EID, "doc_id": "d2", "source": "telegram:ki",
                    "url": "https://t.me/ki/1", "title": "Refinery reportedly burning!",
                    "published_at": iso(T0 + timedelta(minutes=3)), "language": "en",
                    "role": "mention", "added_at": iso(T0)}],
        fields=[{"event_id": EID, "field": "when", "value": "early today", "doc_id": "d1"},
                {"event_id": "ucdp-1", "field": "fatalities_best", "value": "3", "doc_id": "u"}])
    store.save_corroborations([{"event_id": EID, "kind": "fire", "ref_id": "fire:a",
                                "rule": "firms_hotspot", "distance_km": 9.4, "dt_minutes": -70,
                                "score": 0.5, "details": {"frp_mw": 321.0, "lat": 47.83,
                                                          "lon": 39.86, "ts": iso(T0 - timedelta(minutes=70)),
                                                          "satellite": "N21"}}],
                              summaries=[{"event_id": EID, "score": 0.9, "kinds": ["fire"],
                                          "evidence": 1, "corroborated": True}])
    from osint.signals.cache import write_signal
    from tests.intel.conftest import SAT_ROWS
    write_signal(tmp_path, "satellites", {"updated": NOW.timestamp(), "objects": SAT_ROWS})
    ingestor = SimpleNamespace(live={}, _lock=threading.Lock())
    r = SimpleNamespace(store=store, lake=FakeLake(), ingestor=ingestor,
                        settings=SimpleNamespace(data_dir=tmp_path, config_dir=tmp_path,
                                                 api_token=""))
    yield r
    store.close()


@pytest.fixture
def client(rt):
    app = FastAPI()
    mount_intel(app, rt)
    return TestClient(app)


def test_event_intel_all_sections(client):
    j = client.get(f"/api/intel?kind=event&id={EID}").json()
    s = j["sections"]
    assert j["subject"]["status"] == "multi-source" and j["subject"]["radius_km"] == 40.0
    assert set(s) == set(aggregate.SECTIONS)
    assert not [k for k, v in s.items() if isinstance(v, dict) and "error" in v]
    rel = s["related"]
    assert [e["id"] for e in rel["nearest"]] == ["01EVNEAR"] and rel["counts"]["7d"] == 1
    assert len(rel["sparkline"]["days"]) == 30 and rel["same_place"][0]["id"] == "01EVNEAR"
    assert s["history"]["total"] == 1 and s["history"]["fatalities_best"] == 3
    fire = s["sensors"]["fires"]["items"][0]
    assert fire["id"] == "fire:a" and fire["compass"] == "NW" and fire["conflict_score"] is None
    texts = [x["text"] for x in s["assessment"]["items"]]
    assert any("1 fire detection within 9.4 km, earliest 70 min before first report" in t for t in texts)
    assert any("1 independent text" in t for t in texts)  # the Telegram post reposts the article
    assert any(x["level"] == "unconfirmed" and "post-event satellite" in x["text"]
               for x in s["assessment"]["items"])
    assert s["sources"]["groups"] == [[0, 1]] and s["sources"]["by_kind"] == {"rss": 1, "telegram": 1}
    assert {e["name"] for e in s["entities"]["items"]} >= {"Ukraine", "Novoshakhtinsk"}
    assert s["imagery"]["watch"]["state"] == "not_scheduled"
    tl = s["timeline"]
    assert tl["anchor"]["first_report"] == iso(T0) and tl["occurred"]["basis"] == "claims"


def test_sections_filter_actor_point_and_errors(client, rt):
    j = client.get(f"/api/intel?kind=event&id={EID}&sections=related,bogus").json()
    assert list(j["sections"]) == ["related"]
    j = client.get("/api/intel?kind=actor&q=Ukraine").json()
    assert j["sections"]["sensors"] is None and j["sections"]["related"]["total"] == 3
    j = client.get("/api/intel?kind=place&lat=47.8&lon=39.9&label=X&sections=related,entities").json()
    assert j["sections"]["related"]["total"] == 2
    assert client.get("/api/intel?kind=event&id=nope").status_code == 404
    assert client.get("/api/intel?kind=place").status_code == 422
    assert client.get("/api/intel?kind=bogus&lat=1&lon=1").status_code == 422

    def boom(*a, **kw):
        raise RuntimeError("store down")
    hooks.ASSET_PROVIDERS.append(boom)  # a failing feature hook is skipped, not fatal
    try:
        j = client.get("/api/intel?kind=point&lat=47.8&lon=39.9&sections=assets").json()
        assert "error" not in j["sections"]["assets"]
    finally:
        hooks.ASSET_PROVIDERS.remove(boom)
    rt.lake.query = boom  # a failing store yields a section error, not a 500
    j = client.get("/api/intel?kind=point&lat=47.8&lon=39.9&sections=sensors").json()
    assert "store down" in j["sections"]["sensors"]["error"]


def test_track_flaggers_hook_and_live_traffic(client, rt):
    from osint.core.models import Position
    p = Position(track_id="air:abc", kind="aircraft", ts=NOW, lat=47.77, lon=39.94, alt_ft=20000,
                 speed_kts=300.0, heading_deg=90.0, source="t")
    rt.ingestor.live["air:abc"] = p

    def flag(runtime, tracks):
        for t in tracks:
            t["flags"].append({"kind": "sanctions", "label": "Watchlist"})
    hooks.TRACK_FLAGGERS.append(flag)
    try:
        j = client.get(f"/api/intel?kind=event&id={EID}&sections=traffic").json()
    finally:
        hooks.TRACK_FLAGGERS.remove(flag)
    now = j["sections"]["traffic"]["now"]
    assert now["aircraft"] == 1 and now["items"][0]["flags"][0]["kind"] == "sanctions"


def test_timeline_and_exports(client):
    tl = client.get(f"/api/events/{EID}/timeline").json()
    assert [e["kind"] for e in tl["entries"]].count("reported") == 2
    assert client.get("/api/events/nope/timeline").status_code == 404
    r = client.get(f"/api/events/{EID}/export?format=geojson")
    assert r.headers["content-type"] == "application/geo+json"
    assert "attachment" in r.headers["content-disposition"]
    roles = [f["properties"]["role"] for f in r.json()["features"]]
    assert roles == ["event", "evidence"]
    k = client.get(f"/api/events/{EID}/export?format=kml")
    pms = ET.fromstring(k.content).findall(".//{http://www.opengis.net/kml/2.2}Placemark")
    assert len(pms) == 2
    assert client.get(f"/api/events/{EID}/export?format=csv").status_code == 422


def test_repost_groups():
    docs = [{"title": "Refinery reportedly burning after drone attack"},
            {"title": "⚡️Refinery reportedly burning after drone attack."},
            {"title": "Completely different story about ports"}]
    assert repost_groups(docs) == [[0, 1], [2]]
