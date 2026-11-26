"""GET /api/country/{cc}/summary (spec B §2.2): counts, lifecycle, 30-day spark, top actors,
counterparts, bbox / centroid, threat (null when Terror Watch fails), 400 for unknown codes."""

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api import country as country_api
from osint.api.country import mount_country
from osint.core import countries
from osint.signals.countries import Countries
from osint.terror import board
from osint.terror.store import TerrorStore
from tests.country_fixture import T0, build_store, write_geojson

_REAL_BBOX = countries.bbox


class _RT:
    def __init__(self, store, terror_store):
        self.store = store
        self.terror_store = terror_store


@pytest.fixture
def cclient(tmp_path, monkeypatch):
    shapes = Countries(write_geojson(tmp_path / "ne.geojson"))
    monkeypatch.setattr(country_api, "_shapes", lambda: shapes)
    monkeypatch.setattr(countries, "bbox", lambda cc: {"UA": (22.0, 44.0, 40.0, 52.0)}.get(cc))
    store = build_store(tmp_path / "e.sqlite")
    ts = TerrorStore(tmp_path / "terror.sqlite")
    ts.save_level({"country": "UA", "authority": "SBU", "scope": "national", "level": "HIGH",
                   "rank": 3, "scale_max": 4, "as_of": "2026-11-01", "quote": "q",
                   "source_url": "https://x"}, T0)
    ts.save_advisories([{"country": "UA", "level": 4, "label": "Do not travel",
                         "terrorism": True, "risks": ["armed conflict"],
                         "title": "Ukraine Travel Advisory", "published": "2026-10-01",
                         "updated": "2026-10-01", "url": "https://travel.example/ua",
                         "quote": "q"}], T0)
    app = FastAPI()
    mount_country(app, _RT(store, ts), now=lambda: T0 + timedelta(hours=1))
    yield TestClient(app)
    ts.close()
    store.close()


def test_summary_shape(cclient):
    r = cclient.get("/api/country/ua/summary?since=2026-11-20T00:00:00Z")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["cc"] == "UA" and j["name"] == countries.name("UA")
    assert j["counts"] == {"inside": 3, "abroad": 2, "domestic": 1, "foreign": 3,
                           "unattributed": 1}
    assert j["lifecycle"] == {"developing": 1, "active": 2, "cooling": 1, "closed": 1}
    assert len(j["spark"]) == 30
    assert j["spark"][0] == {"day": "2026-10-23", "n": 0}
    assert j["spark"][-1] == {"day": "2026-11-21", "n": 5}
    assert j["top_actors"][0] == {"name": "Ukrainian army", "country": "UA", "n": 3}
    assert [a["name"] for a in j["top_actors"]] == [
        "Ukrainian army", "Polish police", "Russian forces", "Ukrainian police"]
    assert j["counterparts"] == [
        {"cc": "RU", "name": countries.name("RU"), "n": 2, "as_location": 1, "as_actor": 1},
        {"cc": "PL", "name": countries.name("PL"), "n": 1, "as_location": 1, "as_actor": 1}]
    assert j["bbox"] == [22.0, 44.0, 40.0, 52.0] and j["centroid"] == [48.0, 31.0]
    assert j["threat"] == {"level": "HIGH", "source": "SBU", "rank": 3, "scale_max": 4,
                           "as_of": "2026-11-01", "advisories": [
                               {"level": 4, "label": "Do not travel",
                                "title": "Ukraine Travel Advisory",
                                "url": "https://travel.example/ua", "updated": "2026-10-01",
                                "terrorism": True}]}


def test_default_window_category_and_no_bbox(cclient):
    j = cclient.get("/api/country/UA/summary").json()  # default: the last 7 days
    assert j["counts"]["inside"] == 3
    d = cclient.get("/api/country/UA/summary?category=disaster").json()
    assert d["counts"] == {"inside": 1, "abroad": 0, "domestic": 0, "foreign": 0,
                           "unattributed": 1}
    assert d["spark"][-1]["n"] == 5  # the spark ignores the window and the categories
    pl = cclient.get("/api/country/PL/summary").json()
    assert pl["bbox"] is None and pl["centroid"] == [52.0, 19.0]


def test_unknown_country_is_400(cclient):
    r = cclient.get("/api/country/ZZ/summary")
    assert r.status_code == 400 and r.json() == {"error": "unknown_country"}
    assert cclient.get("/api/country/UA/summary?since=yesterday").status_code == 422


def test_threat_null_when_terror_board_fails(cclient, monkeypatch):
    def boom(store, cc):
        raise RuntimeError("terror store unavailable")

    monkeypatch.setattr(board, "threat_section", boom)
    j = cclient.get("/api/country/UA/summary").json()
    assert j["threat"] is None and j["counts"]["inside"] == 3


def test_window_clamped_to_365_days(cclient):
    j = cclient.get("/api/country/UA/summary?since=2020-01-01T00:00:00Z").json()
    assert j["since"].startswith("2025-11-21T13:00:00")  # now - 365 days
    j = cclient.get("/api/country/UA/summary?since=2020-01-01T00:00:00Z"
                    "&until=2026-01-01T00:00:00Z").json()
    assert j["since"].startswith("2025-01-01T00:00:00")  # until - 365 days


@pytest.mark.parametrize("cc", ["RU", "FJ"])
def test_antimeridian_bbox_is_preserved(cclient, monkeypatch, cc):
    monkeypatch.setattr(countries, "bbox", _REAL_BBOX)  # the real, unmocked registry
    b = cclient.get(f"/api/country/{cc}/summary").json()["bbox"]
    assert b is not None and b[0] > b[2]  # west > east: it wraps the date line


def test_shape(cclient):
    r = cclient.get("/api/country/ua/shape")
    assert r.status_code == 200 and r.headers["cache-control"] == "max-age=86400"
    s = r.json()
    assert s["cc"] == "UA" and len(s["polygons"]) == 1 and len(s["polygons"][0]) == 2
    assert s["polygons"][0][0][:4] == [22.0, 44.0, 40.0, 44.0]
    assert cclient.get("/api/country/FR/shape").json()["polygons"] == []
    assert cclient.get("/api/country/ZZ/shape").status_code == 400


def test_country_at_point(cclient):
    assert cclient.get("/api/country/at?lat=48&lon=31").json() == {
        "cc": "UA", "name": countries.name("UA")}
    assert cclient.get("/api/country/at?lat=52&lon=18").json()["cc"] == "PL"
    for lat, lon in ((46.5, 31.0), (0.0, 0.0)):  # inside UA's hole, open sea
        r = cclient.get(f"/api/country/at?lat={lat}&lon={lon}")
        assert r.status_code == 404 and r.json() == {"error": "no_country"}
    assert cclient.get("/api/country/at?lat=95&lon=0").status_code == 422


def test_top_actor_country_normalised_before_migration():
    """Pre-migration rows carry CAMEO / ISO3 actor countries; chips must still get ISO2."""
    ctx = {"location": "inside", "involvement": "domestic"}
    rows = [{"ctx": ctx, "country": "UA", "actor_countries": [], "actors": [
        {"name": "Russian forces", "country": "RUS"},
        {"name": "Ukrainian army", "country": "ukr"},
        {"name": "Polish police", "country": "PL"},
        {"name": "NATO", "country": "IGO"},
        {"name": "Rebels", "country": None}]}]
    top = country_api.summarise("UA", rows, [], T0.date())["top_actors"]
    got = {a["name"]: a["country"] for a in top}
    assert got == {"Russian forces": "RU", "Ukrainian army": "UA", "Polish police": "PL",
                   "NATO": None, "Rebels": None}
