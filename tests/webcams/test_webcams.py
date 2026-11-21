import asyncio
import json
import time

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.webcams import mount_webcams
from osint.webcams import sources as S
from osint.webcams.catalogue import Catalogue
from osint.webcams.settings import WebcamSettings

NOW = "2026-01-01T00:00:00+00:00"
TFL = [
    {"id": "JamCams_00001.1", "commonName": "A406 Test", "lat": 51.6, "lon": -0.01, "additionalProperties": [
        {"key": "available", "value": "true"}, {"key": "imageUrl", "value": "https://img.test/tfl1.jpg"},
        {"key": "videoUrl", "value": "https://img.test/tfl1.mp4"}]},
    {"id": "JamCams_00002.2", "commonName": "Off", "lat": 51.5, "lon": 0.0, "additionalProperties": [
        {"key": "available", "value": "false"}, {"key": "imageUrl", "value": "https://img.test/x.jpg"}]},
]
CAL = {"data": [
    {"cctv": {"index": "1", "inService": "true", "location": {"district": "3", "locationName": "Hwy 5", "latitude": "38.48",
              "longitude": "-121.5"}, "imageData": {"streamingVideoURL": "https://v.test/a.m3u8", "static": {
                  "currentImageURL": "https://img.test/cal.jpg", "currentImageUpdateFrequency": "5"}}}},
    {"cctv": {"index": "2", "inService": "false", "location": {"district": "3", "latitude": "38", "longitude": "-121"},
              "imageData": {"static": {"currentImageURL": "https://img.test/c2.jpg"}}}},
    {"cctv": {"index": "3", "inService": "true", "location": {"district": "3", "latitude": "", "longitude": ""},
              "imageData": {"static": {"currentImageURL": "https://img.test/c3.jpg"}}}},
]}
NYC = [{"id": "abc-1", "name": "Broadway", "latitude": 40.7, "longitude": -74.0, "isOnline": True},
       {"id": "bad/../id", "name": "x", "latitude": 40.7, "longitude": -74.0}]
FI = {"features": [
    {"id": "C1", "geometry": {"coordinates": [24.0, 60.0, 0]}, "properties": {
        "id": "C1", "name": "kt51_Inkoo", "collectionStatus": "GATHERING",
        "presets": [{"id": "C102", "inCollection": True}, {"id": "C101", "inCollection": True}]}},
    {"id": "C2", "geometry": {"coordinates": [24.0, 60.0, 0]}, "properties": {
        "id": "C2", "name": "n", "collectionStatus": "REMOVED_TEMPORARILY", "presets": [{"id": "C201"}]}}]}
SG = {"items": [{"cameras": [{"camera_id": "2701", "image": "https://img.test/sg.jpg",
                              "location": {"latitude": 1.44, "longitude": 103.77}}]}]}
HK = b"""<?xml version="1.0"?><image-list><image><key>H1</key><description>Aberdeen [H1]</description>
<latitude>22.25</latitude><longitude>114.15</longitude><url>https://img.test/H1.JPG</url></image>
<image><key>H2</key><latitude>x</latitude><longitude>1</longitude></image></image-list>"""
WINDY = {"webcams": [{"webcamId": 99, "title": "Beach", "status": "active",
                      "location": {"latitude": 10.0, "longitude": 20.0, "country_code": "pt"},
                      "images": {"current": {"preview": "https://img.test/w.jpg"}},
                      "player": {"live": "https://embed.test/w"}, "urls": {"detail": "https://windy.com/webcams/99"}}]}


def test_parsers():
    t = S.parse_tfl(TFL, NOW)
    assert [w.id for w in t] == ["tfl:00001.1"] and t[0].video_url.endswith(".mp4") and t[0].country == "GB"
    c = S.parse_caltrans(CAL, NOW)
    assert len(c) == 1 and c[0].id == "caltrans:d3-1" and c[0].refresh_s == 300 and c[0].video_url
    n = S.parse_nyc(NYC, NOW)
    assert [w.id for w in n] == ["nyc:abc-1"] and n[0].image_url.endswith("/abc-1/image")
    f = S.parse_digitraffic(FI, NOW)
    assert len(f) == 1 and f[0].image_url == "https://weathercam.digitraffic.fi/C101.jpg" and f[0].name == "kt51 Inkoo"
    assert f[0].licence == "CC BY 4.0"
    s = S.parse_sg(SG, NOW)
    assert s[0].id == "sg:2701" and s[0].lat == 1.44
    h = S.parse_hk(HK, NOW)
    assert len(h) == 1 and h[0].id == "hk:H1" and h[0].lon == 114.15
    w = S.parse_windy(WINDY, NOW)
    assert w[0].id == "windy:99" and w[0].embed_url and w[0].country == "PT" and not w[0].commercial_ok
    assert "windy.com/webcams/99" in w[0].attribution
    assert S.parse_hk(b"<broken", NOW) == [] and S.parse_tfl(None, NOW) == [] and S.parse_sg({}, NOW) == []


def _mock_all(router, windy=False):
    router.get("https://api.tfl.gov.uk/Place/Type/JamCam").respond(json=TFL)
    router.get(host="cwwp2.dot.ca.gov").respond(json=CAL)
    router.get("https://webcams.nyctmc.org/api/cameras").respond(json=NYC)
    router.get("https://tie.digitraffic.fi/api/weathercam/v1/stations").respond(json=FI)
    router.get("https://api.data.gov.sg/v1/transport/traffic-images").respond(json=SG)
    router.get(host="static.data.gov.hk").respond(content=HK)
    if windy:
        router.get(host="api.windy.com").mock(side_effect=[httpx.Response(200, json=WINDY),
                                                           httpx.Response(200, json={"webcams": []})])


ALLOW = {sid: {"allowed_hosts": ["img.test"]} for sid in ("tfl", "caltrans", "nyc", "digitraffic", "sg", "hk", "windy")}
ALLOW["digitraffic"] = {"allowed_hosts": ["weathercam.digitraffic.fi"]}
ALLOW["nyc"] = {"allowed_hosts": ["webcams.nyctmc.org"]}


def cat(tmp_path, **cfg):
    over = cfg.pop("sources", {})
    srcs = {k: {**v, **over.get(k, {})} for k, v in ALLOW.items()}
    config = {"windy_max_pages": 2, **cfg, "sources": srcs}
    return Catalogue(cache_path=tmp_path / "w.json", config=config, settings=WebcamSettings(WINDY_WEBCAMS_KEY=""))


@pytest.fixture
def populated(tmp_path):
    c = cat(tmp_path)
    with respx.mock(assert_all_called=False) as r:
        _mock_all(r)
        meta = asyncio.run(c.refresh())
    return c, meta


def test_refresh_normalises_and_caches(populated, tmp_path):
    c, meta = populated
    # caltrans fetched for 12 districts, same fixture each time -> ids dedupe
    assert meta["tfl"]["count"] == 1 and meta["caltrans"]["count"] == 12 and meta["sg"]["count"] == 1
    assert len(c.items) == 1 + 1 + 1 + 1 + 1 + 1
    assert meta["windy"]["error"].startswith("skipped")
    saved = json.loads((tmp_path / "w.json").read_text())
    assert len(saved["items"]) == len(c.items)
    c2 = cat(tmp_path)
    c2.load_cache()
    assert set(c2.items) == set(c.items) and c2.meta["tfl"]["licence"]


def test_failed_source_keeps_old_items_and_reports_error(populated):
    c, _ = populated
    with respx.mock(assert_all_called=False) as r:
        _mock_all(r)
        r.get("https://api.tfl.gov.uk/Place/Type/JamCam").respond(500)
        meta = asyncio.run(c.refresh())
    assert meta["tfl"]["error"] and meta["tfl"]["count"] == 1 and "tfl:00001.1" in c.items


def test_windy_skipped_without_key_and_used_with_key(tmp_path):
    with respx.mock(assert_all_called=False) as r:
        _mock_all(r, windy=True)
        c = cat(tmp_path)
        asyncio.run(c.refresh())
        assert not any(k.startswith("windy:") for k in c.items) and not r.routes[-1].called
        c2 = Catalogue(cache_path=tmp_path / "w2.json", config={"windy_max_pages": 2, "sources": ALLOW},
                       settings=WebcamSettings(WINDY_WEBCAMS_KEY="k"))
        asyncio.run(c2.refresh())
        assert "windy:99" in c2.items
        assert r.routes[-1].calls[0].request.headers["x-windy-api-key"] == "k"


def test_disabled_source(tmp_path):
    c = cat(tmp_path, sources={"nyc": {"enabled": False}})
    with respx.mock(assert_all_called=False) as r:
        _mock_all(r)
        meta = asyncio.run(c.refresh())
    assert meta["nyc"]["count"] == 0 and meta["nyc"]["enabled"] is False


def test_queries(populated):
    c, _ = populated
    assert {w.id for w in c.query((-1, 51, 1, 52))} == {"tfl:00001.1"}
    assert len(c.query(None, {"hk"})) == 1 and len(c.query(limit=2)) == 2
    near = c.near(51.601, -0.011, 5, 3)
    assert near[0]["id"] == "tfl:00001.1" and near[0]["distance_km"] < 1 and near[0]["has_video"]
    assert c.near(0, 0, 25) == []


def test_cache_staleness(tmp_path):
    c = cat(tmp_path, refresh_hours=6)
    assert c.stale()
    c.fetched_at = time.time()
    assert not c.stale()
    c.fetched_at = time.time() - 7 * 3600
    assert c.stale()


@pytest.fixture
def api(populated):
    c, _ = populated
    c.fetched_at = time.time()
    app = FastAPI()
    mount_webcams(app, c)
    return TestClient(app), c


def test_api_list_detail_near_sources(api):
    cl, _ = api
    j = cl.get("/api/webcams?bbox=-1,51,1,52").json()
    assert j["count"] == 1 and set(j["items"][0]) == {"id", "source", "name", "lat", "lon", "has_video"}
    assert cl.get("/api/webcams?source=hk").json()["count"] == 1
    assert cl.get("/api/webcams?bbox=bad").status_code == 422
    d = cl.get("/api/webcams/tfl:00001.1").json()
    assert d["image_url"] and d["licence"] and d["attribution"]
    assert cl.get("/api/webcams/tfl:nope").status_code == 404
    assert cl.get("/api/webcams/..%2Fetc").status_code in (404, 422)
    n = cl.get("/api/webcams/near?lat=51.6&lon=-0.01&radius_km=10").json()
    assert n["count"] == 1
    s = cl.get("/api/webcams/sources").json()
    assert {x["id"] for x in s["items"]} >= {"tfl", "caltrans", "nyc", "digitraffic", "sg", "hk", "windy"}


def test_snapshot_proxy(api):
    cl, c = api
    with respx.mock(assert_all_called=False) as r:
        route = r.get("https://img.test/tfl1.jpg").respond(content=b"\xff\xd8jpg", headers={"content-type": "image/jpeg"})
        a = cl.get("/api/webcams/tfl:00001.1/snapshot")
        assert a.status_code == 200 and a.content == b"\xff\xd8jpg" and a.headers["content-type"] == "image/jpeg"
        assert "TfL" in a.headers["x-webcam-attribution"]
        cl.get("/api/webcams/tfl:00001.1/snapshot")
        assert route.call_count == 1  # 30 s cache
        c._snaps.clear()
        cl.get("/api/webcams/tfl:00001.1/snapshot")
        assert route.call_count == 2


def test_snapshot_cache_expires(api):
    cl, c = api
    with respx.mock(assert_all_called=False) as r:
        route = r.get("https://img.test/tfl1.jpg").respond(content=b"x", headers={"content-type": "image/png"})
        cl.get("/api/webcams/tfl:00001.1/snapshot")
        k = "tfl:00001.1"
        t, b, ct = c._snaps[k]
        c._snaps[k] = (t - 31, b, ct)
        cl.get("/api/webcams/tfl:00001.1/snapshot")
        assert route.call_count == 2


def test_snapshot_rejects_unknown_and_non_image(api):
    cl, _ = api
    with respx.mock(assert_all_called=False) as r:
        evil = r.get("https://evil.test/x.jpg").respond(content=b"x", headers={"content-type": "image/jpeg"})
        assert cl.get("/api/webcams/tfl:evil/snapshot").status_code == 404
        assert cl.get("/api/webcams/https:%2F%2Fevil.test%2Fx.jpg/snapshot").status_code in (404, 422)
        assert cl.get("/api/webcams/bad id/snapshot").status_code == 422
        assert not evil.called
        r.get("https://img.test/tfl1.jpg").respond(content=b"<html>", headers={"content-type": "text/html"})
        assert cl.get("/api/webcams/tfl:00001.1/snapshot").status_code == 502


def test_snapshot_size_cap_and_upstream_error(api):
    cl, _ = api
    with respx.mock(assert_all_called=False) as r:
        r.get("https://img.test/tfl1.jpg").respond(content=b"x" * (5 * 1024 * 1024 + 1),
                                                   headers={"content-type": "image/jpeg"})
        assert cl.get("/api/webcams/tfl:00001.1/snapshot").status_code == 502
        r.get("https://img.test/tfl1.jpg").respond(404)
        assert cl.get("/api/webcams/tfl:00001.1/snapshot").status_code == 502


def test_snapshot_rejects_svg_and_sets_hardening_headers(api):
    cl, _ = api
    with respx.mock(assert_all_called=False) as r:
        r.get("https://img.test/tfl1.jpg").respond(content=b"<svg onload=alert(1)/>",
                                                   headers={"content-type": "image/svg+xml"})
        assert cl.get("/api/webcams/tfl:00001.1/snapshot").status_code == 502
        r.get("https://img.test/tfl1.jpg").respond(content=b"GIF89a", headers={"content-type": "image/gif"})
        ok = cl.get("/api/webcams/tfl:00001.1/snapshot")
        assert ok.status_code == 200
        assert ok.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in ok.headers["content-security-policy"]


def test_off_list_host_and_http_rejected_at_build(tmp_path):
    c = cat(tmp_path, sources={"tfl": {"allowed_hosts": ["other.test"]}})
    with respx.mock(assert_all_called=False) as r:
        _mock_all(r)
        meta = asyncio.run(c.refresh())
    assert meta["tfl"]["count"] == 0 and meta["tfl"]["rejected"] == 1 and "tfl:00001.1" not in c.items
    c2 = cat(tmp_path)
    w = S.parse_tfl(TFL, NOW)[0]
    assert c2.image_allowed(w)
    w.image_url = "http://img.test/tfl1.jpg"
    assert not c2.image_allowed(w)
    c3 = cat(tmp_path, sources={"tfl": {"http_only": True}})
    assert c3.image_allowed(w)
    w.image_url = "https://user@img.test/x.jpg"
    assert not c2.image_allowed(w)
    assert not Catalogue(cache_path=tmp_path / "n.json", config={}).image_allowed(w)  # no list: nothing allowed


def test_snapshot_cache_capped_by_bytes(tmp_path):
    c = Catalogue(cache_path=tmp_path / "w.json", config={"snapshot_cache_mb": 1})
    mb = 1024 * 1024
    for i in range(3):
        c._cache_put(f"a:{i}", b"x" * (mb // 2 - 10), "image/png")
    assert list(c._snaps) == ["a:1", "a:2"] and c._snap_total <= mb
    c._cache_put("a:big", b"x" * (mb + 1), "image/png")  # larger than the whole cap: not cached
    assert "a:big" not in c._snaps and c._snap_total <= mb
