import io
import json
import zipfile

import httpx

from osint.infra import datasets as D

CABLE_GEO = {"features": [
    {"properties": {"id": "c-1", "name": "Cable One", "color": "#ff0000"},
     "geometry": {"type": "MultiLineString", "coordinates": [[[10.123456, 55.0], [11.0, 55.5]], [[1, 1]]]}},
    {"properties": {"id": "bad"}, "geometry": {"type": "Point", "coordinates": [0, 0]}},
]}
LANDING_GEO = {"features": [
    {"properties": {"id": "l-1", "name": "Cork, Ireland", "is_tbd": False},
     "geometry": {"type": "Point", "coordinates": [-8.47, 51.89]}}]}
OVERPASS_PIPES = {"elements": [
    {"type": "way", "id": 7, "tags": {"name": "Europipe", "substance": "gas", "location": "underwater"},
     "geometry": [{"lat": 55.0, "lon": 5.0}, {"lat": 55.00001, "lon": 5.5}, {"lat": 55.0, "lon": 6.0}]},
    {"type": "way", "id": 8, "tags": {"seamark:type": "pipeline_submarine",
                                      "seamark:pipeline_submarine:product": "oil"},
     "geometry": [{"lat": 1, "lon": 1}, {"lat": 2, "lon": 2}]},
    {"type": "way", "id": 9, "tags": {}, "geometry": [{"lat": 1, "lon": 1}]},
]}
TARGETS = ('"id","schema","name","aliases","birth_date","countries","addresses","identifiers","sanctions",'
           '"phones","emails","program_ids","dataset","first_seen","last_seen","last_change"\n'
           '"p1","Person","X","","","","","","","","","","","","",""\n'
           '"v1","Vessel","Nurkez","","","","","352002470;IMO9253325","EU","","","EU-RUS","EU","","",""\n'
           '"a1","Airplane","EP-MOF","","","","","149","US","","","US-IRAN","OFAC","","",""\n')


def test_norm_cables():
    d = D.norm_cables(CABLE_GEO, LANDING_GEO)
    assert len(d["cables"]) == 1
    c = d["cables"][0]
    assert c["name"] == "Cable One" and c["lines"] == [[[10.1235, 55.0], [11.0, 55.5]]]
    assert d["landings"] == [{"id": "l-1", "name": "Cork, Ireland", "lat": 51.89, "lon": -8.47, "tbd": False}]


def test_norm_pipelines_simplifies_and_classifies():
    items = D.norm_pipelines(OVERPASS_PIPES)
    assert [p["id"] for p in items] == ["osm:w7", "osm:w8"]
    assert items[0]["line"] == [[5.0, 55.0], [6.0, 55.0]]  # near-collinear middle vertex dropped
    assert items[0]["offshore"] and items[0]["substance"] == "gas"
    assert items[1]["substance"] == "oil" and items[1]["offshore"]


def test_norm_refineries_and_wikidata():
    r = D.norm_refineries({"elements": [{"type": "way", "id": 3, "center": {"lat": 1.5, "lon": 2.5},
                                          "tags": {"name": "R", "operator": "O"}},
                                         {"type": "way", "id": 4, "tags": {}}]})
    assert r == [{"id": "osm:w3", "name": "R", "operator": "O", "lat": 1.5, "lon": 2.5,
                  "product": None, "wikidata": None}]
    b = lambda **kw: {k: {"value": v} for k, v in kw.items()}
    w = D.norm_wikidata([
        b(i="http://www.wikidata.org/entity/Q1", label="Dam A", coord="Point(10.5 45.25)", height="120"),
        b(i="http://www.wikidata.org/entity/Q1", label="Dam A", coord="Point(10.5 45.25)", country="Italy"),
        b(i="http://www.wikidata.org/entity/Q2", coord="Point(500 45)"),
    ])
    assert len(w) == 1 and w[0]["height_m"] == 120.0 and w[0]["country"] == "Italy"
    assert w[0]["lat"] == 45.25 and w[0]["id"] == "wd:Q1"


def test_filter_os_targets():
    out = io.StringIO()
    n = D.filter_os_targets(io.StringIO(TARGETS), out)
    assert n == 2
    body = out.getvalue()
    assert "Nurkez" in body and "EP-MOF" in body and "Person" not in body


def _handler(request: httpx.Request) -> httpx.Response:
    u = str(request.url)
    if u.endswith("cable-geo.json"):
        return httpx.Response(200, json=CABLE_GEO)
    if u.endswith("landing-point-geo.json"):
        return httpx.Response(200, json=LANDING_GEO)
    if "msi.nga.mil" in u:
        return httpx.Response(503, text="<!DOCTYPE html><html>sorry</html>")
    if "rimes" in u:
        return httpx.Response(200, text="_id,Main Port Name,Latitude,Longitude\n1,Foo,1,2\n")
    if u.endswith(".zip"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("global_power_plant_database.csv", "name,latitude,longitude\na,1,2\n")
        return httpx.Response(200, content=buf.getvalue())
    return httpx.Response(404)


def test_refresh_skips_fresh_and_records_meta(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    res = D.refresh(tmp_path, ["cables", "wpi", "power", "nope"], client=client)
    assert res["cables"]["ok"] and res["cables"]["count"] == 1 and res["cables"]["landings"] == 1
    assert res["wpi"]["ok"]  # NGA 503 apology page -> mirror
    assert res["power"]["ok"] and (tmp_path / "gppd.csv").is_file()
    assert res["nope"] == {"ok": False, "error": "unknown dataset"}
    meta = json.loads((tmp_path / D.META_FILE).read_text())
    assert set(meta) == {"cables", "wpi", "power"}
    again = D.refresh(tmp_path, ["cables"], client=client)
    assert again["cables"]["skipped"] == "fresh"
    assert not D.stale(tmp_path, D.DATASETS["cables"]) and D.stale(tmp_path, D.DATASETS["pipelines"])
    client.close()


def test_refresh_failure_keeps_previous_file(tmp_path):
    (tmp_path / "cables_tg.json").write_text('{"cables": [], "landings": []}')
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    res = D.refresh(tmp_path, ["cables"], force=True, client=client)
    assert not res["cables"]["ok"] and "HTTP 500" in res["cables"]["error"]
    assert (tmp_path / "cables_tg.json").read_text().startswith('{"cables"')
    client.close()
