import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.infra import mount_infra
from osint.api.licences import licences
from osint.infra.job import WatchStore
from osint.infra.loader import Infra

GPPD = ("country,country_long,name,gppd_idnr,capacity_mw,latitude,longitude,primary_fuel,other_fuel1,"
        "other_fuel2,other_fuel3,commissioning_year,owner,source,url\n"
        "FRA,France,Gravelines,FRA001,5460,51.015,2.136,Nuclear,,,,1980,EDF,x,https://example.org/g\n"
        "DEU,Germany,Tiny PV,DEU9,0.5,50,8,Solar,,,,,,,\n"
        "XXX,Nowhere,Bad,X1,1,,,Gas,,,,,,,\n")
AIRPORTS = ("id,ident,type,name,latitude_deg,longitude_deg,elevation_ft,continent,iso_country,iso_region,"
            "municipality,scheduled_service,gps_code,iata_code,local_code,home_link,wikipedia_link,keywords\n"
            "1,H1,heliport,City Heliport,10,20,5,EU,FR,,Paris,no,,,,,,\n"
            "2,H2,heliport,Army Air Field Helipad,10.5,20.5,5,EU,FR,,,no,,,,,,\n"
            "3,S1,small_airport,Farm Strip,40,40,5,EU,FR,,,no,,,,,,\n"
            "4,L1,large_airport,Big,10.2,20.2,5,EU,FR,,,yes,,,,,,\n")


@pytest.fixture
def client(tmp_path):
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "gppd.csv").write_text(GPPD, encoding="utf-8")
    (ref / "airports.csv").write_text(AIRPORTS, encoding="utf-8")
    (ref / "cables_tg.json").write_text(json.dumps({"cables": [{"id": "c", "name": "C", "color": None,
                                                                "lines": [[[0, 0], [1, 1]]]}],
                                                    "landings": [{"id": "l", "name": "L", "lat": 0, "lon": 0}]}))
    (ref / "infra_wikidata.json").write_text(json.dumps({"dams": [{"id": "wd:Q1", "name": "D", "lat": 1, "lon": 1}]}))
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "watchlist.yaml").write_text("entries:\n  - mmsi: '1'\n    label: X\n", encoding="utf-8")
    wpath = tmp_path / "w.sqlite"
    store = WatchStore(wpath)
    store.set_active([{"track_id": "sea:1", "severity": "high", "name": "X", "lat": 1, "lon": 2,
                       "ts": "2026-11-23T12:00:00+00:00", "hits": [{"entry": {"id": "e"}}]}])
    for i in range(3):
        store.add_alert({"ts": "2026-11-23T12:00:00+00:00", "kind": "watchlist", "track_id": "sea:1",
                         "severity": "high", "title": f"t{i}"})
    store.set_meta("status", {"matched": 1})
    app = FastAPI()
    rt = SimpleNamespace(settings=SimpleNamespace(config_dir=cfg, data_dir=tmp_path))
    mount_infra(app, rt, infra=Infra(ref), watch_path=wpath)
    with TestClient(app) as c:
        yield c
    store.close()


def test_points_and_gzip(client):
    r = client.get("/api/infra/points/power", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    j = r.json()
    assert j["count"] == 2
    g = {x["name"]: x for x in j["items"]}
    assert g["Gravelines"]["group"] == "nuclear" and g["Gravelines"]["mw"] == 5460 and g["Gravelines"]["year"] == 1980
    assert g["Tiny PV"]["url"] is None
    raw = client.get("/api/infra/points/power", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in raw.headers and raw.json()["count"] == 2
    assert client.get("/api/infra/points/dams").json()["count"] == 1
    assert client.get("/api/infra/points/lng").json()["count"] == 0  # missing in file: empty
    assert client.get("/api/infra/points/nope").status_code == 404
    c = client.get("/api/infra/cables").json()
    assert c["count"] == 1 and c["landings"][0]["name"] == "L"
    assert client.get("/api/infra/pipelines").json()["count"] == 0


def test_airfields(client):
    j = client.get("/api/infra/airfields?bbox=19,9,21,11").json()
    assert {a["id"] for a in j["items"]} == {"H1", "H2"} and not j["truncated"]
    j = client.get("/api/infra/airfields?bbox=19,9,21,11&military_only=true").json()
    assert [a["id"] for a in j["items"]] == ["H2"]
    assert client.get("/api/infra/airfields?bbox=-180,-90,180,90&type=small_airport").json()["count"] == 1
    assert client.get("/api/infra/airfields?bbox=-180,-90,180,90&limit=1").json()["truncated"]
    assert client.get("/api/infra/airfields?bbox=x").status_code == 422
    assert client.get("/api/infra/airfields?bbox=0,0,1,1&type=balloon").status_code == 422


def test_summary_and_watch(client):
    s = client.get("/api/infra/summary").json()
    assert s["layers"]["power"] == {"available": True, "count": 2}
    assert s["layers"]["pipelines"]["available"] is False
    assert s["datasets"]["opensanctions"]["stale"] is True
    assert {x["id"] for x in s["sources"]} >= {"cables", "opensanctions"}
    assert s["watch"]["status"] == {"matched": 1}
    a = client.get("/api/watch/active").json()
    assert a["count"] == 1 and a["items"][0]["hits"][0]["entry"]["id"] == "e"
    al = client.get("/api/watch/alerts").json()["items"]
    assert [x["title"] for x in al] == ["t2", "t1", "t0"]
    assert client.get(f"/api/watch/alerts?since_id={al[1]['id']}").json()["count"] == 1
    w = client.get("/api/watch/list").json()
    assert w["user"]["entries"][0]["label"] == "X" and w["commercial_ok"] is False


def test_watch_without_store(tmp_path):
    app = FastAPI()
    mount_infra(app, None, infra=Infra(tmp_path), watch_path=tmp_path / "missing.sqlite")
    with TestClient(app) as c:
        assert c.get("/api/watch/active").json() == {"items": [], "count": 0, "status": None}
        assert c.get("/api/watch/alerts").json()["count"] == 0


def test_licences_include_infra():
    ids = {s["id"]: s for s in licences()["sources"]}
    assert ids["opensanctions"]["commercial"] == "no" and ids["cables"]["commercial"] == "no"
    assert ids["pipelines"]["share_alike"] is True and ids["power"]["commercial"] == "ok"
