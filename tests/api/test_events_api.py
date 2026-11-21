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
    r = client.get("/api/events")
    assert r.status_code == 200 and r.json()["count"] == 3
    item = r.json()["items"][0]
    assert set(item) == {"id", "lat", "lon", "precision", "category", "status", "confidence",
                         "title", "mention_count", "source_diversity", "t_start", "t_end",
                         "updated_at"}
    assert ids(client.get("/api/events?bbox=30,40,40,60")) == {"1"}
    assert ids(client.get("/api/events?bbox=170,0,-170,20")) == {"3"}  # antimeridian
    assert ids(client.get("/api/events?category=civil_unrest,disaster")) == {"2", "3"}
    assert ids(client.get("/api/events?status=multi-source")) == {"1"}
    assert ids(client.get("/api/events?min_confidence=0.55")) == {"1", "3"}
    assert ids(client.get("/api/events?since=2026-11-21T00:00:00Z")) == {"1", "3"}
    assert client.get("/api/events?limit=1").json()["count"] == 1
    assert client.get("/api/events?bbox=bad").status_code == 422
    assert client.get("/api/events?since=yesterday").status_code == 422
    assert client.get("/api/events?min_confidence=2").status_code == 422


def test_detail(client):
    eid = "01EV" + "1".zfill(22)
    d = client.get(f"/api/events/{eid}").json()
    assert d["gdelt_ids"] == ["77"] and d["actors"][0]["name"] == "Russia"
    assert d["documents"][0]["url"] == "https://bbc.example/a"
    assert d["fields"] == [{"field": "location", "value": "Kharkiv", "doc_id": "doc:a",
                            "quote": "in Kharkiv"}]
    assert "cluster_state" not in d
    assert client.get("/api/events/nope").status_code == 404


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
