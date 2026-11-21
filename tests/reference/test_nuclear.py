import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.nuclear import mount_nuclear
from osint.reference import nuclear


def _b(qid, kind, label, lon, lat, **kw):
    row = {"i": {"value": "http://www.wikidata.org/entity/" + qid}, "kind": {"value": kind},
           "label": {"value": label}, "coord": {"value": f"Point({lon} {lat})"}}
    for k, v in kw.items():
        row[k] = {"value": v}
    return row


BINDINGS = [
    # duplicates the curated Bushehr record (same place, similar name) -> merged into curated
    _b("Q204637", "power", "Bushehr Nuclear Power Plant", 50.8860, 28.8300, operator="NPPD",
       capacity_mw="1000", service="2011-09-03T00:00:00Z", state_of_use="in use"),
    _b("Q1", "power", "Biblis Nuclear Power Plant", 8.415, 49.71, state_of_use="decommissioned",
       capacity_mw="2525", country="Germany"),
    _b("Q1", "facility", "Biblis Nuclear Power Plant", 8.415, 49.71),  # same item, less specific
    _b("Q2", "reactor", "Biblis A", 8.416, 49.711),  # unit of the plant -> dropped
    _b("Q3", "research", "FRM II", 11.676, 48.266, service="2004-03-02T00:00:00Z"),
    _b("Q4", "test_site", "Some test site", 10.0, 10.0, dissolved="1990-01-01T00:00:00Z"),
    _b("Q5", "power", "Never built", 1.0, 1.0, state_of_use="cancelled"),  # not a site
    _b("Q6", "enrichment", "Gronau enrichment plant", 7.03, 52.21,
       state_of_use="building or structure under construction"),
    {"i": {"value": "bad"}},  # malformed row is skipped
]
CURATED = [
    {"name": "Bushehr Nuclear Power Plant", "type": "power", "status": "operating", "country": "Iran",
     "operator": None, "capacity_mw": None, "lat": 28.8297, "lon": 50.8858,
     "source_url": "https://en.wikipedia.org/wiki/Bushehr_Nuclear_Power_Plant"},
    {"name": "Bogus", "type": "not-a-type", "lat": 0, "lon": 0},
]


@pytest.fixture
def sites(tmp_path):
    (tmp_path / "nuclear_wikidata.json").write_text(json.dumps({"results": {"bindings": BINDINGS}}), encoding="utf-8")
    cur = tmp_path / "curated.json"
    cur.write_text(json.dumps(CURATED), encoding="utf-8")
    return nuclear.NuclearSites(tmp_path, cur)


def test_merge_curated_wins_and_folds_wikidata(sites):
    by_name = {x["name"]: x for x in sites.items}
    bushehr = [x for x in sites.items if "Bushehr" in x["name"]]
    assert len(bushehr) == 1 and bushehr[0]["source"] == "Curated"
    assert bushehr[0]["wikidata"] == "Q204637" and bushehr[0]["operator"] == "NPPD"
    assert bushehr[0]["capacity_mw"] == 1000
    assert "Biblis A" not in by_name and "Never built" not in by_name and "Bogus" not in by_name
    biblis = by_name["Biblis Nuclear Power Plant"]
    assert biblis["type"] == "power" and biblis["status"] == "decommissioned"
    assert biblis["licence"].startswith("CC0") and biblis["source_url"].endswith("/Q1")
    assert by_name["FRM II"]["status"] == "operating" and by_name["FRM II"]["type"] == "research"
    assert by_name["Some test site"]["status"] == "shut_down"
    assert by_name["Gronau enrichment plant"]["status"] == "under_construction"


def test_missing_wikidata_file_is_tolerated(tmp_path):
    s = nuclear.NuclearSites(tmp_path)
    assert s.items and all(x["source"] == "Curated" for x in s.items)
    assert all(x["source_url"].startswith("https://") for x in s.items)


def test_curated_file_is_valid():
    rows = json.loads((nuclear.Path(nuclear.__file__).with_name("nuclear_curated.json")).read_text(encoding="utf-8"))
    assert len(rows) >= 30
    for r in rows:
        assert r["type"] in nuclear.TYPES and -90 <= r["lat"] <= 90 and -180 <= r["lon"] <= 180
        assert r["source_url"].startswith("https://en.wikipedia.org/wiki/")


def test_similar_names():
    assert nuclear.similar("Fukushima Daiichi Nuclear Power Plant", "Fukushima I NPP")
    assert not nuclear.similar("Biblis Nuclear Power Plant", "Philippsburg Nuclear Power Plant")


def test_api(sites):
    app = FastAPI()
    mount_nuclear(app, sites)
    c = TestClient(app)
    j = c.get("/api/reference/nuclear").json()
    assert j["count"] == len(sites.items)
    keys = {"id", "name", "type", "status", "country", "operator", "capacity_mw", "lat", "lon",
            "source", "source_url", "licence"}
    assert keys <= set(j["items"][0])
    j = c.get("/api/reference/nuclear?type=power&bbox=0,20,60,60").json()
    assert {x["name"] for x in j["items"]} == {"Bushehr Nuclear Power Plant", "Biblis Nuclear Power Plant"}
    assert c.get("/api/reference/nuclear?type=fuel_cycle").json()["count"] == 1  # Gronau
    assert c.get("/api/reference/nuclear?type=test").json()["count"] == 1
    assert c.get("/api/reference/nuclear?type=nope").status_code == 422
    assert c.get("/api/reference/nuclear?bbox=bad").status_code == 422
