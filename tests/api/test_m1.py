import xml.etree.ElementTree as ET
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api import layers, m1
from osint.api.m1 import mount_m1
from osint.config import Settings
from osint.core.geo import utcnow
from osint.core.models import Observation, Position, TrackInfo
from osint.core.scheduler import SourceHealth
from osint.runtime import Runtime
from osint.store.lake import observation_row

NS = {"k": "http://www.opengis.net/kml/2.2"}


@pytest.fixture
def rt(tmp_path):
    r = Runtime(Settings(_env_file=None, data_dir=tmp_path), [])
    now = utcnow()
    live = r.ingestor.live
    live["air:a"] = Position(track_id="air:a", kind="air", ts=now, lat=10.123456789, lon=20.0,
                             source="t", alt_ft=30000, speed_kts=400, heading_deg=90)
    live["air:old"] = Position(track_id="air:old", kind="air", ts=now - timedelta(hours=1),
                               lat=10, lon=20, source="t")
    live["air:b"] = Position(track_id="air:b", kind="air", ts=now, lat=-40, lon=100, source="t")
    live["sea:1"] = Position(track_id="sea:1", kind="sea", ts=now - timedelta(hours=2),
                             lat=1, lon=2, source="t")
    r.store.upsert_tracks([
        TrackInfo(track_id="air:a", kind="air", ident="a", name="RCH<1>&", is_mil=True, reg="05-5140",
                  high_priority=True, type_code="C17", extra={"sq": "7700"}),
        TrackInfo(track_id="sea:1", kind="sea", ident="1", name="HMS Test", is_mil=True),
    ], now)
    r.scheduler.health["adsb"] = SourceHealth(status="healthy", last_success=now,
                                              last_batch_size=3)
    obs = [
        Observation(id="fire:1", type="fire", ts=now - timedelta(hours=1), lat=5, lon=6,
                    source="f", value=12.0,
                    attrs={"confidence": "h", "satellite": "N", "daynight": "D"}),
        Observation(id="fire:old", type="fire", ts=now - timedelta(hours=30), lat=5, lon=6,
                    source="f", value=1.0),
        Observation(id="quake:1", type="quake", ts=now - timedelta(hours=2), lat=1, lon=2,
                    source="u", value=5.0, version="1", attrs={"place": "old place"}),
        Observation(id="quake:1", type="quake", ts=now - timedelta(hours=2), lat=1, lon=2,
                    source="u", value=5.4, version="2",
                    attrs={"place": "X & <Y>", "depth_km": 10, "event_type": "earthquake",
                           "url": "http://u/1"}),
    ]
    r.lake.write("observations", [observation_row(o) for o in obs])
    yield r
    r.store.close()


@pytest.fixture
def client(rt):
    app = FastAPI()
    mount_m1(app, rt)
    return TestClient(app)


def test_config(client, rt):
    local = TestClient(client.app, client=("127.0.0.1", 50000))
    assert local.get("/api/config").json() == {"cesium_ion_token": None}
    rt.settings.cesium_ion_token = "tok"
    assert local.get("/api/config").json() == {"cesium_ion_token": "tok"}
    assert client.get("/api/config").json() == {"cesium_ion_token": None}  # non-loopback peer, no API token


def test_cesium_token_from_env(monkeypatch):
    monkeypatch.setenv("CESIUM_ION_TOKEN", "envtok")
    assert Settings(_env_file=None).cesium_ion_token == "envtok"


def test_aircraft_layer(client):
    d = client.get("/api/layers/aircraft").json()
    assert d["layer"] == "aircraft" and d["count"] == 2
    a = next(i for i in d["items"] if i["id"] == "air:a")
    assert a["lat"] == 10.12346 and a["is_mil"] is True and a["high_priority"] is True
    assert a["type_code"] == "C17" and a["reg"] == "05-5140" and "extra" not in a
    b = next(i for i in d["items"] if i["id"] == "air:b")
    assert b["name"] is None and b["is_mil"] is False and b["reg"] is None


def test_track_detail(client):
    d = client.get("/api/track/air:a").json()
    assert d["track"]["extra"] == {"sq": "7700"} and d["track"]["reg"] == "05-5140"
    assert d["position"]["alt_ft"] == 30000
    assert client.get("/api/track/air:b").json()["track"] is None
    assert client.get("/api/track/nope").status_code == 404


def test_layer_served_from_cache(client, rt):
    first = client.get("/api/layers/aircraft").json()
    rt.ingestor.live.pop("air:b")
    assert client.get("/api/layers/aircraft").json() == first  # within the 5 s TTL


def test_bbox_filter(client):
    d = client.get("/api/layers/aircraft?bbox=0,0,30,30").json()
    assert [i["id"] for i in d["items"]] == ["air:a"]


def test_ships_window(client):
    d = client.get("/api/layers/ships").json()
    assert d["count"] == 1 and d["items"][0]["name"] == "HMS Test"


def test_fires_window(client):
    d = client.get("/api/layers/fires").json()
    assert [i["id"] for i in d["items"]] == ["fire:1"]
    assert d["items"][0]["frp"] == 12.0 and d["items"][0]["satellite"] == "N"
    assert client.get("/api/layers/fires?hours=48").json()["count"] == 2
    assert client.get("/api/layers/fires?hours=999").status_code == 422
    assert d["truncated"] is False


def test_fires_truncated(client, monkeypatch):
    monkeypatch.setattr(layers, "OBS_LIMIT", 1)
    d = client.get("/api/layers/fires?hours=48").json()
    assert d["count"] == 1 and d["truncated"] is True
    assert client.get("/api/layers/fires").json()["truncated"] is False


def test_quake_version_dedupe(client):
    d = client.get("/api/layers/quakes").json()
    assert d["count"] == 1
    q = d["items"][0]
    assert q["mag"] == 5.4 and q["place"] == "X & <Y>" and q["depth_km"] == 10


def test_status(client):
    d = client.get("/api/status").json()
    assert d["counts"] == {"aircraft": 2, "ships": 1}  # air:old is past the 30 min max age
    assert d["sources"]["adsb"]["status"] == "healthy"
    assert d["sources"]["adsb"]["last_batch_size"] == 3


def _parse(resp):
    assert resp.headers["content-type"].startswith("application/vnd.google-earth.kml+xml")
    return ET.fromstring(resp.content)


def test_master_kml(client):
    root = _parse(client.get("/kml/master.kml"))
    links = root.findall(".//k:NetworkLink", NS)
    assert len(links) == 5
    hrefs = [link.find("k:Link/k:href", NS).text for link in links]
    assert hrefs[0] == "http://testserver/kml/aircraft.kml"
    assert hrefs[-1] == "http://testserver/kml/events.kml"
    assert all("token" not in h for h in hrefs)
    assert [link.find("k:Link/k:refreshInterval", NS).text for link in links] == [
        "30", "60", "600", "120", "120"]


def test_aircraft_kml_escaping_and_style(client):
    root = _parse(client.get("/kml/aircraft.kml"))
    pms = root.findall(".//k:Placemark", NS)
    assert len(pms) == 2
    pm = next(p for p in pms if p.find("k:name", NS).text == "RCH<1>&")
    assert pm.find(".//k:altitudeMode", NS).text == "absolute"
    assert pm.find(".//k:coordinates", NS).text.startswith("20.0,10.12346,9144")
    assert pm.find(".//k:IconStyle/k:color", NS).text == "ff0000ff"
    assert pm.find(".//k:heading", NS).text == "90"
    assert "RCH&lt;1&gt;&amp;" in pm.find("k:description", NS).text


def test_other_kml(client):
    for path, name in (("ships", "HMS Test"), ("fires", "FRP 12 MW"), ("quakes", "M5.4")):
        root = _parse(client.get(f"/kml/{path}.kml"))
        pms = root.findall(".//k:Placemark", NS)
        assert len(pms) == 1 and pms[0].find("k:name", NS).text == name
    assert "X &amp; &lt;Y&gt;" in root.find(".//k:Placemark/k:description", NS).text


def test_static_fallback_and_frontend(client, monkeypatch, tmp_path):
    monkeypatch.setattr(m1, "FRONTEND", tmp_path / "nofront")
    assert client.get("/").json()["service"] == "osint"
    fe = tmp_path / "fe"
    fe.mkdir()
    (fe / "index.html").write_text("<html>hi</html>")
    monkeypatch.setattr(m1, "FRONTEND", fe)
    assert client.get("/").text == "<html>hi</html>"
