import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.events import events_kml, mount_events
from osint.store.sqlite import SqliteStore, iso

NS = {"k": "http://www.opengis.net/kml/2.2"}
T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _event(i, *, lat, lon, category="armed_conflict", status="reported", conf=0.5,
           updated=T0, title=None):
    return {"id": f"01EV{i:022d}", "category": category, "title": title or f"Event {i}",
            "summary": "s", "actors": [{"name": "Russia", "role": "source", "type": "RUS"}],
            "lat": lat, "lon": lon, "precision": "city", "place_name": "P",
            "country": "UA", "geo_confidence": 0.9, "h3_3": "831196fffffffff",
            "t_start": iso(T0 - timedelta(hours=1)), "t_end": iso(T0), "status": status,
            "confidence": conf, "version": 2, "mention_count": 2,
            "source_diversity": 2 if status == "multi-source" else 1, "gdelt_ids": ["77"],
            "first_seen": iso(T0), "updated_at": iso(updated),
            "cluster_state": {"words": ["w:x"]}}


class _RT:
    def __init__(self, store):
        self.store = store


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "e.sqlite")
    s.save_events(
        [_event(1, lat=50.0, lon=36.0, status="multi-source", conf=0.9),
         _event(2, lat=-12.0, lon=-77.0, category="civil_unrest", conf=0.3,
                updated=T0 - timedelta(days=2)),
         _event(3, lat=10.0, lon=179.5, category="disaster", conf=0.6,
                title="Fire & <flood> \"]]> 'x'")],
        documents=[{"event_id": _event(1, lat=0, lon=0)["id"], "doc_id": "doc:a",
                    "source": "rss:bbc", "url": "https://bbc.example/a", "title": "A",
                    "published_at": iso(T0), "language": "en", "role": "primary",
                    "added_at": iso(T0)}],
        fields=[{"event_id": _event(1, lat=0, lon=0)["id"], "field": "location",
                 "value": "Kharkiv", "doc_id": "doc:a", "quote": "in Kharkiv"}],
        decisions=[{"gdelt_event_id": "77", "decision": "accepted",
                    "reason": "duplicate_of_event", "event_id": "x", "doc_id": "doc:g",
                    "category": "armed_conflict", "decided_at": iso(T0)},
                   {"gdelt_event_id": "78", "decision": "rejected", "reason": "low_mentions",
                    "decided_at": iso(T0)}])
    yield s
    s.close()


@pytest.fixture
def client(store):
    app = FastAPI()
    mount_events(app, _RT(store))
    return TestClient(app)


def ids(resp):
    return {it["id"][-1] for it in resp.json()["items"]}


def test_list_slim_and_filters(client):
    r = client.get("/api/events?all=true")
    assert r.status_code == 200 and r.json()["count"] == 3
    item = r.json()["items"][0]
    assert set(item) >= {"id", "lat", "lon", "precision", "category", "status", "confidence",
                          "title", "mention_count", "source_diversity", "t_start", "t_end",
                          "updated_at", "place_name", "country"}
    assert item["place_name"] == "P" and item["country"] == "UA"
    assert ids(client.get("/api/events?bbox=30,40,40,60")) == {"1"}
    assert ids(client.get("/api/events?bbox=170,0,-170,20")) == {"3"}  # antimeridian
    assert ids(client.get("/api/events?all=true&category=civil_unrest,disaster")) == {"2", "3"}
    assert ids(client.get("/api/events?status=multi-source")) == {"1"}
    assert ids(client.get("/api/events?min_confidence=0.55")) == {"1", "3"}
    assert ids(client.get("/api/events?all=1&since=2026-11-21T00:00:00Z")) == {"1", "3"}
    assert client.get("/api/events?limit=1").json()["count"] == 1
    assert client.get("/api/events?bbox=bad").status_code == 422
    assert client.get("/api/events?since=yesterday").status_code == 422
    assert client.get("/api/events?min_confidence=2").status_code == 422


def test_list_default_map_view_and_unlocated(client, store):
    # default: multi-source, or reported with city/point precision and confidence >= 0.5
    assert ids(client.get("/api/events")) == {"1", "3"}
    un = _event(4, lat=None, lon=None, status="multi-source", conf=0.9)
    un["precision"] = "unlocated"
    country = _event(5, lat=48.0, lon=31.0, conf=0.9)
    country["precision"] = "country"
    store.save_events([un, country])
    assert ids(client.get("/api/events")) == {"1", "3"}
    assert ids(client.get("/api/events?include_unlocated=true")) == {"1", "3", "4"}
    assert ids(client.get("/api/events?all=true")) == {"1", "2", "3", "4", "5"}


def test_detail(client):
    eid = "01EV" + "1".zfill(22)
    d = client.get(f"/api/events/{eid}").json()
    assert d["gdelt_ids"] == ["77"] and d["actors"][0]["name"] == "Russia"
    assert d["documents"][0]["url"] == "https://bbc.example/a"
    assert d["fields"] == [{"field": "location", "value": "Kharkiv", "doc_id": "doc:a",
                            "quote": "in Kharkiv"}]
    assert "cluster_state" not in d
    assert client.get("/api/events/nope").status_code == 404


def test_detail_of_a_merged_event_resolves_to_its_survivor(client, store):
    e1, e2 = "01EV" + "1".zfill(22), "01EV" + "2".zfill(22)
    assert store.merge_events([{"survivor": e1, "loser": e2, "similarity": 0.81}], []) == 1
    d = client.get(f"/api/events/{e2}").json()
    assert d["id"] == e1 and d["redirected_from"] == e2
    assert d["documents"][0]["doc_id"] == "doc:a"


def test_stats(client):
    s = client.get("/api/events/stats").json()
    assert s["events"] == 3
    assert s["by_status"] == {"multi-source": 1, "reported": 2}
    assert s["by_category"]["disaster"] == 1
    assert s["gdelt"]["accepted"]["by_reason"] == {"duplicate_of_event": 1}
    assert s["gdelt"]["rejected"]["total"] == 1 and s["gdelt"]["acceptance_rate"] == 0.5


def test_kml_escapes_and_colours(client):
    r = client.get("/kml/events.kml")
    assert r.status_code == 200 and "kml" in r.headers["content-type"]
    root = ET.fromstring(r.content)  # well-formed despite the hostile title
    pms = root.findall(".//k:Placemark", NS)
    assert len(pms) == 3
    names = {p.find("k:name", NS).text for p in pms}
    assert "Fire & <flood> \"]]> 'x'" in names
    colors = {p.find(".//k:IconStyle/k:color", NS).text for p in pms}
    assert "ff0000ff" in colors  # armed_conflict red


def test_kml_skips_unlocated():
    out = events_kml([{"id": "x", "lat": None, "lon": None}])
    assert "<Placemark>" not in out
    ET.fromstring(out)


def test_corroboration_fields(client, store):
    eid = _event(1, lat=0, lon=0)["id"]
    store.save_corroborations(
        [{"event_id": eid, "kind": "fire", "ref_id": "fire:1", "rule": "firms_hotspot",
          "distance_km": 3.2, "dt_minutes": 0.0, "score": 0.6, "details": {"frp_mw": 40}}],
        summaries=[{"event_id": eid, "score": 0.6, "kinds": ["fire"], "evidence": 1,
                    "corroborated": True}])
    sensor = _event(9, lat=33.0, lon=44.0, status="sensor-corroborated", conf=0.8)
    store.save_sensor_event(sensor, {"detector": "orbit", "dedupe_key": "orbit:x:1",
                                     "cell": "x"},
                            [{"event_id": sensor["id"], "kind": "aircraft", "ref_id": "air:1",
                              "rule": "orbit", "score": 0.7}],
                            {"event_id": sensor["id"], "score": 0.8, "kinds": ["aircraft"],
                             "evidence": 1, "corroborated": True})
    items = {it["id"][-1]: it for it in client.get("/api/events?all=true").json()["items"]}
    assert items["1"]["corroboration_score"] == 0.6
    assert items["1"]["corroboration_kinds"] == ["fire"] and items["1"]["source"] == "news"
    assert items["2"]["corroboration_score"] == 0.0 and items["2"]["corroboration_kinds"] == []
    assert items["9"]["source"] == "sensor" and items["9"]["detector"] == "orbit"
    d = client.get(f"/api/events/{eid}").json()
    [c] = d["corroborations"]
    assert (c["kind"], c["ref_id"], c["rule"], c["distance_km"], c["dt_minutes"],
            c["score"], c["details"]) == ("fire", "fire:1", "firms_hotspot", 3.2, 0.0, 0.6,
                                          {"frp_mw": 40})
    s = client.get("/api/events/stats").json()
    assert s["by_status"]["sensor-corroborated"] == 1
    assert s["sensor"]["corroborations_by_kind"] == {"fire": 1, "aircraft": 1}
    assert s["sensor"]["news_events_sensor_corroborated"] == 1
    assert s["sensor"]["sensor_derived"] == {"total": 1, "by_detector": {"orbit": 1}}


def test_verification_fields_and_unsupported_hidden(client, store):
    e1, e3 = _event(1, lat=0, lon=0)["id"], _event(3, lat=0, lon=0)["id"]
    store.save_verifications([
        {"event_id": e1, "verdict": "supported", "reasons": [],
         "quotes": [{"field": "casualties", "quote": "two killed", "verbatim": True}],
         "fingerprint": "a"},
        {"event_id": e3, "verdict": "unsupported", "reasons": ["opinion piece"],
         "quotes": [], "fingerprint": "b"}])
    assert ids(client.get("/api/events")) == {"1"}  # unsupported leaves the map view
    items = {it["id"][-1]: it for it in client.get("/api/events?all=true").json()["items"]}
    assert items["1"]["verdict"] == "supported" and items["3"]["verdict"] == "unsupported"
    assert items["2"]["verdict"] is None
    d = client.get(f"/api/events/{e1}").json()
    assert d["verification"] == {"verdict": "supported", "reasons": [], "quotes": [
        {"field": "casualties", "quote": "two killed", "verbatim": True}]}
    assert client.get(f"/api/events/{_event(2, lat=0, lon=0)['id']}").json()[
        "verification"] is None


from osint.api.events import field_precedence

RULE_ROWS = [
    {"field": "headline", "value": "Missile hits Kharkiv", "doc_id": "d1", "quote": "x"},
    {"field": "summary", "value": "A missile hit Kharkiv.", "doc_id": "d1", "quote": "x"},
    {"field": "casualties.killed", "value": "12", "doc_id": "d2", "quote": "12 killed"},
    {"field": "weapons_platforms", "value": "missile", "doc_id": "d1", "quote": "missile"},
    {"field": "weapons_platforms", "value": "drone", "doc_id": "d2", "quote": "drone"}]


def _ext(verdict, **result):
    return {"status": "done", "verdict": verdict, "result": {
        "summary": "LLM summary.", "casualties": {"killed": 14, "injured": None},
        "weapons_platforms": [], **result}}


def test_supported_llm_values_outrank_rule_values():
    out = field_precedence(RULE_ROWS, _ext("supported"))
    assert out["extracted"] == {"headline": "Missile hits Kharkiv", "summary": "LLM summary.",
                                "casualties": {"killed": 14, "injured": None},
                                "weapons_platforms": ["drone", "missile"]}
    assert out["field_source"] == {"headline": "rule", "summary": "llm",
                                   "casualties.killed": "llm", "casualties.injured": None,
                                   "weapons_platforms": "rule"}


def test_rule_values_without_a_supported_extraction():
    for ext in (None, _ext("mismatch"), _ext("unsupported"),
                {"status": "error", "verdict": None, "result": None}):
        out = field_precedence(RULE_ROWS, ext)
        assert out["extracted"]["summary"] == "A missile hit Kharkiv."
        assert out["extracted"]["casualties"]["killed"] == 12
        assert set(out["field_source"].values()) <= {"rule", None}
    empty = field_precedence([], None)
    assert empty["extracted"]["weapons_platforms"] == [] and empty["extracted"]["headline"] is None


def test_a_malformed_stored_rule_count_is_no_value():
    rows = [{"field": "casualties.killed", "value": "12a", "doc_id": "d", "quote": "x"},
            {"field": "casualties.injured", "value": "", "doc_id": "d", "quote": "x"}]
    out = field_precedence(rows, None)
    assert out["extracted"]["casualties"] == {"killed": None, "injured": None}
    assert out["field_source"]["casualties.killed"] is None


def test_llm_counts_are_coerced_and_a_missing_one_falls_back_to_the_rule():
    rows = [{"field": "casualties.injured", "value": "7", "doc_id": "d", "quote": "7 hurt"}]
    out = field_precedence(rows, _ext("supported", casualties={"killed": "12",
                                                               "injured": None}))
    assert out["extracted"]["casualties"] == {"killed": 12, "injured": 7}
    assert out["field_source"]["casualties.killed"] == "llm"
    assert out["field_source"]["casualties.injured"] == "rule"
    bad = field_precedence(rows, _ext("supported", casualties={"killed": "many",
                                                               "injured": "lots"}))
    assert bad["extracted"]["casualties"] == {"killed": None, "injured": 7}
    assert bad["field_source"]["casualties.killed"] is None
    assert bad["field_source"]["casualties.injured"] == "rule"


class _Ext:
    def __init__(self, recs):
        self.recs = recs

    def get(self, eid):
        return self.recs.get(eid)

    def get_many(self, ids):
        return {i: self.recs[i] for i in ids if i in self.recs}


def test_event_detail_reports_extracted_fields_and_their_source(store):
    eid = _event(1, lat=0, lon=0)["id"]
    store.replace_rule_fields([eid], [{**r, "event_id": eid} for r in RULE_ROWS])
    rt = _RT(store)
    rt.extraction_store = _Ext({eid: _ext("supported")})
    app = FastAPI()
    mount_events(app, rt)
    d = TestClient(app).get(f"/api/events/{eid}").json()
    assert d["extracted"]["casualties"]["killed"] == 14
    assert d["field_source"]["casualties.killed"] == "llm"
    assert d["field_source"]["headline"] == "rule"
    rt.extraction_store = None
    d = TestClient(app).get(f"/api/events/{eid}").json()
    assert d["extracted"]["casualties"]["killed"] == 12
    assert d["field_source"]["casualties.killed"] == "rule"
