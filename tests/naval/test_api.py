"""End-to-end: synthetic lake + store -> CarrierJob -> /api/naval/* envelopes."""

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.naval import mount_naval
from osint.collectors.nga_msi import parse_warning
from osint.config import ROOT, Settings
from osint.core.geo import utcnow
from osint.core.models import Position, TrackInfo
from osint.naval.config import NavalConfig
from osint.naval.job import DISCLAIMER, CarrierJob
from osint.naval.store import NavalStore
from osint.store.lake import Lake, document_row, position_row
from osint.store.sqlite import SqliteStore
from tests.naval.helpers import synthetic_geo

TEXT = ("1. AIRCRAFT CARRIER FLIGHT OPERATIONS IN AREA BOUND BY 01-00.00N 008-30.00E, "
        "01-00.00N 009-30.00E, 00-00.00N 009-30.00E, 00-00.00N 008-30.00E.")


@pytest.fixture
def env(tmp_path):
    now = utcnow()
    settings = Settings(_env_file=None, data_dir=tmp_path, config_dir=ROOT / "config")
    store = SqliteStore(settings.sqlite_path)
    lake = Lake(settings.lake_dir)
    pos = []
    # C-2 descending and vanishing far east of the synthetic island
    for i, alt in enumerate((9000, 5000, 2500, 1000)):
        pos.append(Position(track_id="air:c2", kind="air", ts=now - timedelta(minutes=90 - i * 5),
                            lat=0.2, lon=8.8 + i * 0.02, alt_ft=alt, speed_kts=140,
                            source="adsb:test"))
    # fleet oiler loitering for 14 h
    for h in range(14):
        pos.append(Position(track_id="sea:1", kind="sea", ts=now - timedelta(hours=14 - h),
                            lat=0.0 + (h % 3) * 0.01, lon=9.0 + (h % 2) * 0.01, speed_kts=6.0,
                            source="aisstream"))
    lake.write("positions", [position_row(p) for p in pos])
    store.upsert_tracks([
        TrackInfo(track_id="air:c2", kind="air", ident="c2", type_code="C2", name="RAWHIDE1",
                  is_mil=True),
        TrackInfo(track_id="sea:1", kind="sea", ident="1", name="USNS PECOS", type_code="31"),
    ], now)
    doc = parse_warning({"msgYear": 2026, "msgNumber": 1, "navArea": "9", "text": TEXT,
                         "status": "A", "issueDate": ""}, now, ["CARRIER", "FLIGHT OPERATIONS"], [])
    lake.write("documents", [document_row(doc)])
    store.set_cursor("nga_msi", {"active": ["9-1-2026"]})
    nstore = NavalStore(tmp_path / "naval.sqlite")
    nstore.set_meta("usni", {"as_of": now.date().isoformat(), "url": "https://usni.test/a",
                             "fetched_at": now.isoformat(), "entries": [
                                 {"carrier": "USS Alpha", "hull": "CVN-1", "region": "Test Sea",
                                  "status": "at_sea", "lat": 0.5, "lon": 9.5}]})
    job = CarrierJob(lake, store, settings, NavalConfig.load(ROOT / "config" / "naval.yaml"),
                     nstore, geo_factory=synthetic_geo)
    app = FastAPI()
    mount_naval(app, None, job)
    try:
        yield TestClient(app), job
    finally:
        nstore.close()
        store.close()
        lake.close()


def test_carriers_envelope_and_detail(env):
    client, job = env
    r = client.get("/api/naval/carriers?hours=48")
    assert r.status_code == 200
    body = r.json()
    assert body["layer"] == "naval_carriers" and body["disclaimer"] == DISCLAIMER
    assert body["count"] == len(body["items"]) == 1
    h = body["items"][0]
    assert set(h["kinds"]) == {"sea_terminating_flight", "cvw_aircraft", "support_ship",
                               "nav_warning", "air_track"}
    # the air-track fit is built from the same C-2 flight: never an extra independent signal
    assert set(h["independent_kinds"]) == {"sea_terminating_flight", "support_ship",
                                           "nav_warning"}
    fit = h["air_track"]
    assert fit["aircraft"] == ["air:c2"] and fit["fixes"][0]["role"] == "recovery"
    assert "RAWHIDE1" in fit["fixes"][0]["description"]
    assert "carrier onboard delivery" in fit["fixes"][0]["label"]
    assert h["ellipse"]["semi_major_km"] >= h["ellipse"]["semi_minor_km"] > 0
    assert h["confidence"] > 0.5 and h["radius_km"] >= 50 and h["label"].startswith("CSG? ")
    assert h["candidate_names"][0]["name"] == "USS Alpha"
    st = body["indicator_status"]
    assert st["traffic_void"]["note"] == "" or "baseline" in st["traffic_void"]["note"]
    assert st["support_ship"]["evidence"] == 1 and st["nav_warning"]["evidence"] == 1
    d = client.get(f"/api/naval/carriers/{h['id']}").json()
    assert d["id"] == h["id"] and len(d["history"]) == 1 and d["disclaimer"] == DISCLAIMER
    assert client.get("/api/naval/carriers/csg-0000000000").status_code == 404
    assert client.get("/api/naval/carriers/not-an-id").status_code == 404
    # a second pass keeps the identity and extends the history
    job.compute(48)
    again = client.get("/api/naval/carriers").json()["items"][0]
    assert again["id"] == h["id"] and again["first_seen"] == h["first_seen"]


def test_surface_indicators_eval(env):
    client, _ = env
    s = client.get("/api/naval/carrier-surface?hours=48").json()
    assert s["layer"] == "naval_surface" and s["count"] > 0
    cell = s["items"][0]
    assert set(cell) >= {"h3", "boundary", "p"} and cell["p"] == 1.0
    ind = client.get("/api/naval/indicators?kind=nav_warning").json()
    assert ind["count"] == 1 and ind["items"][0]["polygon"]
    allk = client.get("/api/naval/indicators").json()
    assert {i["kind"] for i in allk["items"]} >= {"sea_terminating_flight", "support_ship",
                                                   "usni_prior"}
    at = client.get("/api/naval/air-tracks").json()
    assert at["layer"] == "naval_air_tracks" and at["count"] == 1
    assert at["items"][0]["ellipse"]["confidence"] == 0.95 and at["disclaimer"] == DISCLAIMER
    ev = client.get("/api/naval/eval").json()
    assert ev["available"] and ev["at_sea_carriers"] == 1 and ev["recall_with_slack"] == 1.0
    assert client.get("/api/naval/carriers?hours=0").status_code == 422
    assert client.get("/api/naval/carriers?hours=500").status_code == 422
