"""Imagery viewer catalogue (STAC fixtures, Maxar static catalogue, Wayback walk) and API."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.imagery import mount_imagery
from osint.api.imagery_viewer import mount_imagery_viewer
from osint.imagery import catalog as C
from osint.imagery.viewer import ViewerService

LAT, LON = 47.7604, 39.9334
NOW = datetime(2026, 11, 23, 12, tzinfo=UTC)


def s2_feature(sid, when, cloud, bbox, platform="sentinel-2b"):
    a = {k: {"href": f"https://cogs.test/{sid}/{k}.tif",
             "raster:bands": [{"scale": 0.0001, "offset": -0.1}]} for k in C.S2_BANDS}
    a["cloud"] = {"href": "s3://bucket/cloud.jp2"}
    return {"id": sid, "collection": "sentinel-2-l2a", "bbox": bbox,
            "properties": {"datetime": when, "eo:cloud_cover": cloud, "platform": platform,
                           "proj:epsg": 32637, "s2:mgrs_tile": sid[4:9]}, "assets": a}


def test_parse_s2_dedupes_tiles_of_one_pass_and_keeps_alternates():
    deep = s2_feature("S2B_37TEN_X", "2026-11-13T08:27:00Z", 3.0, [39.0, 47.0, 41.0, 48.5])
    edge = s2_feature("S2B_37UEP_X", "2026-11-13T08:26:46Z", 9.0, [39.9, 47.7, 41.0, 48.5])
    other = s2_feature("S2C_37TEN_Y", "2026-11-10T08:27:00Z", 50.0, [39.0, 47.0, 41.0, 48.5],
                       platform="sentinel-2c")
    scenes = C._dedupe([C.parse_s2(f) for f in (edge, deep, other)], LAT, LON)
    assert sorted(s["id"] for s in scenes) == ["S2B_37TEN_X", "S2C_37TEN_Y"]
    kept = next(s for s in scenes if s["id"] == "S2B_37TEN_X")
    assert kept["platform"] == "Sentinel-2B" and kept["alts"][0]["id"] == "S2B_37UEP_X"
    assert kept["assets"]["red"]["scale_offset"] == (0.0001, -0.1)
    pub = C.public(kept)
    assert "scl" in kept["assets"] and "alts" not in pub and "assets" not in pub
    assert pub["alt_keys"] == ["s2:S2B_37UEP_X"]
    assert kept["attribution"] == "Contains modified Copernicus Sentinel data 2026"


def test_parse_s1_single_pol_drops_vh_presets():
    f = {"id": "S1A_X_rtc", "collection": "sentinel-1-rtc", "bbox": [39, 47, 41, 49],
         "properties": {"datetime": "2026-11-16T03:30:55Z", "platform": "sentinel-1a",
                        "sar:polarizations": ["VV"], "sat:orbit_state": "descending",
                        "sat:relative_orbit": 21}}
    s = C.parse_s1(f)
    assert s["presets"] == ["sar", "sar_change"] and s["extra"]["calibrated"]
    assert C.parse_s1({**f, "properties": {**f["properties"], "sar:polarizations": ["HH"]}}) is None


@respx.mock
def test_search_s1_lists_grd_only_newer_than_rtc():
    def feats(coll, items):
        return {"features": [{"id": i, "collection": coll, "bbox": [39, 47, 41, 49],
                              "properties": {"datetime": t, "platform": "sentinel-1c",
                                             "sar:polarizations": ["VV", "VH"]}} for i, t in items]}

    def search(request):
        body = __import__("json").loads(request.content)
        if body["collections"] == ["sentinel-1-rtc"]:
            return httpx.Response(200, json=feats("sentinel-1-rtc", [("R1", "2026-11-16T03:30:55Z")]))
        assert body["datetime"].startswith("2026-11-16T03:30:55Z/")
        return httpx.Response(200, json=feats("sentinel-1-grd", [("G1", "2026-11-22T03:30:39Z"),
                                                                 ("G0", "2026-11-16T03:30:55Z")]))

    respx.post(f"{C.PC_STAC}/search").mock(side_effect=search)
    with httpx.Client() as c:
        rtc, grd = C.search_s1(c, LAT, LON, NOW - timedelta(days=30), NOW)
    assert [s["key"] for s in rtc] == ["s1:R1"] and [s["key"] for s in grd] == ["s1grd:G1"]
    assert grd[0]["extra"]["calibrated"] is False and grd[0]["renderer"] == "titiler"


@respx.mock
def test_maxar_index_walks_only_matching_events():
    root = C.MAXAR_ROOT
    respx.get(root + "catalog.json").mock(return_value=httpx.Response(200, json={"links": [
        {"rel": "child", "href": "./Here/collection.json"}, {"rel": "child", "href": "./Far/collection.json"}]}))
    respx.get(root + "Here/collection.json").mock(return_value=httpx.Response(200, json={
        "id": "Here", "title": "Event here", "extent": {"spatial": {"bbox": [[39, 47, 41, 48]]}},
        "links": [{"rel": "child", "href": "./ard/acquisition_collections/A_collection.json"}]}))
    far = respx.get(root + "Far/collection.json").mock(return_value=httpx.Response(200, json={
        "id": "Far", "extent": {"spatial": {"bbox": [[0, 0, 1, 1]]}},
        "links": [{"rel": "child", "href": "./ard/acquisition_collections/B_collection.json"}]}))
    respx.get(root + "Here/ard/acquisition_collections/A_collection.json").mock(
        return_value=httpx.Response(200, json={"links": [{"rel": "item", "href": "../37/q1/2023-09-13/A.json"},
                                                         {"rel": "item", "href": "../37/q2/2023-09-13/A.json"}]}))
    item = lambda bbox: {"bbox": bbox, "properties": {
        "datetime": "2023-09-13 09:18:09Z", "platform": "GE01", "gsd": 0.42, "tile:clouds_percent": 2,
        "proj:epsg": 32637, "view:off_nadir": 6.0}, "assets": {"visual": {"href": "./A-visual.tif"}}}
    respx.get(root + "Here/ard/37/q1/2023-09-13/A.json").mock(return_value=httpx.Response(200, json=item([39.9, 47.7, 40.0, 47.8])))
    respx.get(root + "Here/ard/37/q2/2023-09-13/A.json").mock(return_value=httpx.Response(200, json=item([41.0, 48.0, 41.1, 48.1])))
    with httpx.Client() as c:
        found = C.MaxarIndex().search(c, LAT, LON)
    assert len(found) == 1 and far.called
    s = found[0]
    assert s["key"] == "maxar:Here/ard/37/q1/2023-09-13/A" and s["gsd_m"] == 0.42
    assert s["assets"]["visual"]["href"] == root + "Here/ard/37/q1/2023-09-13/A-visual.tif"
    assert "CC BY-NC" in s["attribution"] and s["acquired_at"] == "2023-09-13T09:18:09Z"


@respx.mock
def test_wayback_walk_metadata_dedupe_and_disk_cache(tmp_path):
    cfg = {str(r): {"itemTitle": f"World Imagery (Wayback {d})", "metadataLayerUrl": f"https://meta.test/{r}"}
           for r, d in ((10, "2026-03-26"), (20, "2025-04-24"), (30, "2023-02-23"), (40, "2014-02-20"))}
    respx.get(C.WAYBACK_CONFIG).mock(return_value=httpx.Response(200, json=cfg))
    # newest release's tile was last changed in release 20; 20's predecessor 30 -> 40 (oldest)
    sel = {10: 20, 30: 40, 40: 40}
    respx.get(url__startswith="https://wayback.maptiles.arcgis.com/").mock(side_effect=lambda req: httpx.Response(
        200, json={"data": [1], "select": [sel[int(req.url.path.split("/")[7])]]}))
    meta = {20: {"SRC_DATE": 20220629, "SRC_DESC": "WV02", "SRC_RES": 0.5, "NICE_DESC": "Vantor"},
            40: {"SRC_DATE": 20220629, "SRC_DESC": "WV02", "SRC_RES": 0.5, "NICE_DESC": "Maxar"}}
    respx.get(url__startswith="https://meta.test/20/").mock(return_value=httpx.Response(200, json={"features": [{"attributes": meta[20]}]}))
    respx.get(url__startswith="https://meta.test/40/").mock(return_value=httpx.Response(504))
    wb = C.Wayback(cache_dir=tmp_path)
    with httpx.Client() as c:
        out = wb.search(c, LAT, LON)
    assert [s["id"] for s in out] == ["20", "40"]
    assert out[0]["acquired_at"] == "2022-06-29T00:00:00Z" and out[0]["platform"] == "WV02"
    assert out[0]["extra"]["release_date"] == "2025-04-24" and out[0]["extra"]["provider"] == "Vantor"
    # metadata timed out: listed by its release date, flagged as such
    assert out[1]["acquired_at"] == "2014-02-20T00:00:00Z" and not out[1]["extra"]["capture_date_known"]
    assert "{z}" in out[0]["extra"]["tiles"] and "/tile/20/" in out[0]["extra"]["tiles"]
    respx.reset()
    with httpx.Client() as c:  # second search: from the disk cache, no requests
        assert [s["id"] for s in C.Wayback(cache_dir=tmp_path).search(c, LAT, LON)] == ["20", "40"]


class _Cat(C.Catalogue):
    def _run(self, src, client, lat, lon, start, end):
        if src == "maxar":
            raise httpx.ConnectError("down")
        if src == "s2":
            return [C.scene("s2", "S2B_A", "sentinel-2-l2a", "2026-11-13T09:31:39Z", cloud_pct=4.0)]
        return []


def _app(tmp_path):
    runtime = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path, api_token="", config_dir=None),
                              store=None)
    svc = ViewerService(tmp_path, client_factory=httpx.Client)
    svc.catalogue = _Cat(httpx.Client)
    runtime.imagery_viewer = svc
    app = FastAPI()
    mount_imagery_viewer(app, runtime)
    mount_imagery(app, runtime)
    return TestClient(app), svc


def test_api_scenes_reports_per_source_errors_and_caches(tmp_path):
    client, svc = _app(tmp_path)
    r = client.get("/api/imagery/scenes", params={"lat": LAT, "lon": LON, "sources": "s2,maxar"})
    assert r.status_code == 200
    j = r.json()
    assert [s["key"] for s in j["scenes"]] == ["s2:S2B_A"] and "dnbr" in j["presets"]
    st = {s["id"]: s for s in j["sources"]}
    assert st["s2"]["count"] == 1 and "ConnectError" in st["maxar"]["error"]
    assert svc.catalogue.get("s2:S2B_A") is not None
    assert client.get("/api/imagery/scenes", params={"lat": 99, "lon": 0}).status_code == 422
    bad = client.get("/api/imagery/scenes", params={"lat": LAT, "lon": LON, "from": "2026-11-20", "to": "2026-11-01"})
    assert bad.status_code == 422


def test_api_render_errors_are_data_and_view_serves_cache(tmp_path):
    client, svc = _app(tmp_path)
    r = client.get("/api/imagery/render", params={"key": "s2:unknown", "preset": "true", "lat": LAT, "lon": LON})
    assert r.status_code == 200 and "unknown scene" in r.json()["error"]
    assert client.get("/api/imagery/render", params={"key": "bad key", "preset": "true", "lat": LAT, "lon": LON}).status_code == 422
    svc.dir.mkdir(parents=True)
    (svc.dir / ("a" * 24 + ".webp")).write_bytes(b"RIFF....WEBP")
    v = client.get("/api/imagery/view/" + "a" * 24 + ".webp", params={"dl": "S2B 2026/11.png"})
    assert v.status_code == 200 and v.headers["content-type"] == "image/webp"
    assert 'filename="S2B_2026_11.png"' in v.headers["content-disposition"]
    assert client.get("/api/imagery/view/" + "b" * 24 + ".webp").status_code == 404
    assert client.get("/api/imagery/view/../x.webp").status_code == 404
