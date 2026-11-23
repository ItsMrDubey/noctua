import json
import time
from types import SimpleNamespace

import httpx
import pytest
import respx

from osint.collectors.adsb_signals import AdsbSignalsCollector
from osint.collectors.celestrak import GP_URL, CelestrakCollector
from osint.collectors.ioda import API as IODA_API
from osint.collectors.ioda import IodaCollector
from osint.collectors.radiation import BFS_URL, SAFECAST_URL, RadiationCollector
from osint.collectors.swpc import AURORA, BASE, PRODUCTS, SwpcCollector
from osint.core.collector import CollectorContext
from osint.signals import countries as countries_mod
from osint.signals.cache import SignalReader
from osint.store.sqlite import SqliteStore


@pytest.fixture
def ctx_factory(tmp_path):
    stores = []

    def make(http):
        store = SqliteStore(tmp_path / "s.sqlite")
        stores.append(store)
        return CollectorContext(http=http, settings=SimpleNamespace(data_dir=tmp_path),
                                store=store)
    yield make
    for s in stores:
        s.close()


def read(tmp_path, name):
    return SignalReader(tmp_path).get(name)


async def nosleep(_s):
    return None


# ------------------------------------------------------------------ adsb_signals

BASE_FI = "https://fi.test/api/v2"


def aircraft(hex_code, lat, lon, **kw):
    return {"hex": hex_code, "type": "adsb_icao", "lat": lat, "lon": lon, "alt_baro": 30000,
            "version": 2, "nic": 8, "nac_p": 10, "seen_pos": 0.5, **kw}


@respx.mock
async def test_adsb_signals_poll(tmp_path, ctx_factory):
    now = time.time()
    region = [aircraft("a1", 34.70, 33.0, nic=0, nac_p=0),
              aircraft("a2", 34.71, 33.01, gpsOkBefore=now - 30),
              aircraft("a3", 34.72, 33.02),
              aircraft("a4", 34.70, 33.03, squawk="7700", flight="RYR681H ", t="B738")]
    respx.get(f"{BASE_FI}/lat/34/lon/33/dist/250").mock(
        return_value=httpx.Response(200, json={"now": now, "aircraft": region}))
    respx.get(f"{BASE_FI}/lat/55/lon/20/dist/100").mock(return_value=httpx.Response(503))
    respx.get(f"{BASE_FI}/sqk/7700").mock(return_value=httpx.Response(200, json={
        "now": now * 1000, "ac": [aircraft("a4", 34.70, 33.03, squawk="7700",
                                           flight="RYR681H ")]}))
    respx.get(f"{BASE_FI}/sqk/7600").mock(return_value=httpx.Response(200, json={"ac": []}))
    cfg = {"base": BASE_FI, "squawks": ["7700", "7600"], "gnss": {"min_aircraft": 3},
           "regions": [{"name": "EastMed", "lat": 34, "lon": 33, "dist": 250},
                       {"name": "Baltic", "lat": 55, "lon": 20, "dist": 100}]}
    async with httpx.AsyncClient() as http:
        c = AdsbSignalsCollector(cfg)
        c.sleep = nosleep
        ctx = ctx_factory(http)
        batch = await c.poll(ctx)
        again = await c.poll(ctx)
    g = read(tmp_path, "gnss")
    assert g["samples"] == 5 and len(g["cells"]) == 1
    cell = g["cells"][0]
    assert (cell["n"], cell["bad"], cell["level"]) == (4, 2, "high")
    regions = {r["name"]: r for r in g["regions"]}
    assert regions["EastMed"]["aircraft"] == 4 and regions["Baltic"]["ok"] is False
    sq = read(tmp_path, "squawks")["items"]
    assert len(sq) == 1 and sq[0]["code"] == "7700" and sq[0]["flight"] == "RYR681H"
    assert sq[0]["type"] == "B738"
    obs = [o for o in batch.observations if o.type == "squawk"]
    assert len(obs) == 1 and obs[0].attrs["severity"] == "high"
    assert [o.id for o in again.observations] == [obs[0].id]  # same episode, same id
    assert ctx.get_cursor("adsb_signals")["episodes"][0]["code"] == "7700"


@respx.mock
async def test_adsb_signals_rate_limit_and_total_failure(tmp_path, ctx_factory):
    respx.get(f"{BASE_FI}/lat/1/lon/2/dist/250").mock(return_value=httpx.Response(429))
    respx.get(f"{BASE_FI}/sqk/7700").mock(return_value=httpx.Response(200, json={"ac": []}))
    cfg = {"base": BASE_FI, "squawks": ["7700"], "regions": [{"lat": 1, "lon": 2}]}
    async with httpx.AsyncClient() as http:
        c = AdsbSignalsCollector(cfg)
        c.sleep = nosleep
        with pytest.raises(RuntimeError, match="rate limited"):
            await c.poll(ctx_factory(http))  # 429 on the first request ends the cycle


def test_adsb_signals_closed_hours_become_observations():
    from osint.collectors.adsb_signals import gnss_observation

    o = gnss_observation(1795406400, "833d8bfffffffff", 10, 4, 3)
    assert o.id == "gnss:833d8bfffffffff:1795406400" and o.value == 40.0
    assert o.attrs == {"cell": "833d8bfffffffff", "res": 3, "n": 10, "bad": 4}


# ------------------------------------------------------------------ celestrak

OMM = {"OBJECT_NAME": "ISS (ZARYA)", "OBJECT_ID": "1998-067A", "EPOCH": "2026-11-22T21:08:45",
       "MEAN_MOTION": 15.49, "ECCENTRICITY": 0.0007, "INCLINATION": 51.63,
       "RA_OF_ASC_NODE": 234.76, "ARG_OF_PERICENTER": 54.9, "MEAN_ANOMALY": 305.2,
       "NORAD_CAT_ID": 25544, "BSTAR": 0.00013, "MEAN_MOTION_DOT": 7e-5,
       "MEAN_MOTION_DDOT": 0}
STAR = {**OMM, "OBJECT_NAME": "STARLINK-1007", "NORAD_CAT_ID": 44713}


@respx.mock
async def test_celestrak_groups_derived_and_refresh_policy(tmp_path, ctx_factory):
    active = respx.get(GP_URL.format(group="active")).mock(
        return_value=httpx.Response(200, json=[OMM, STAR]))
    stations = respx.get(GP_URL.format(group="stations")).mock(
        return_value=httpx.Response(200, json=[OMM]))
    cfg = {"groups": ["active", "stations"], "derived": {"starlink": "STARLINK"}}
    async with httpx.AsyncClient() as http:
        c = CelestrakCollector(cfg)
        c.sleep = nosleep
        ctx = ctx_factory(http)
        await c.poll(ctx)
        snap = read(tmp_path, "satellites")
        assert snap["membership"] == {"active": [25544, 44713], "stations": [25544],
                                      "starlink": [44713]}
        assert snap["objects"]["44713"][1] == "STARLINK-1007"
        await c.poll(ctx)  # within min_refresh_s: nothing fetched again
    assert active.call_count == 1 and stations.call_count == 1


@respx.mock
async def test_celestrak_unchanged_403_keeps_cache(tmp_path, ctx_factory):
    respx.get(GP_URL.format(group="stations")).mock(return_value=httpx.Response(
        403, text="GP data has not updated since your last successful\ndownload of "
                  "GROUP=stations at 2026-11-23 04:16:04 UTC.\nData is updated every 2 hours."))
    async with httpx.AsyncClient() as http:
        c = CelestrakCollector({"groups": ["stations"], "derived": {}})
        await c.poll(ctx_factory(http))  # not an error
    snap = read(tmp_path, "satellites")
    assert "has not updated" in snap["groups"]["stations"]["unchanged"]


@respx.mock
async def test_celestrak_all_failed_raises(ctx_factory):
    respx.get(GP_URL.format(group="stations")).mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError):
            await CelestrakCollector({"groups": ["stations"]}).poll(ctx_factory(http))


# ------------------------------------------------------------------ ioda


@respx.mock
async def test_ioda_poll(tmp_path, ctx_factory, monkeypatch):
    monkeypatch.setattr(countries_mod, "_cache", SimpleNamespace(
        point=lambda cc: {"IR": (32.0, 53.0, "Iran")}.get(cc)))
    now = int(time.time())
    route = respx.get(f"{IODA_API}/outages/events").mock(return_value=httpx.Response(
        200, json={"data": [
            {"entity": {"code": "IR", "name": "Iran"}, "from": now - 7200, "until": now - 60,
             "score": 40000, "datasource": "bgp"},
            {"entity": {"code": "ZZ", "name": "Nowhere"}, "from": 1, "until": 2,
             "score": 4000, "datasource": "bgp"}]}))
    async with httpx.AsyncClient() as http:
        batch = await IodaCollector({}).poll(ctx_factory(http))
    assert route.calls[0].request.url.params["entityType"] == "country"
    assert [o.id for o in batch.observations] == [f"outage:IR:bgp:{now - 7200}"]
    assert batch.observations[0].attrs["notable"] is True
    snap = read(tmp_path, "outages")
    assert snap["countries"][0]["cc"] == "IR" and snap["countries"][0]["lat"] == 32.0
    assert len(snap["events"]) == 1


# ------------------------------------------------------------------ swpc


@respx.mock
async def test_swpc_poll_tolerates_a_failed_product(tmp_path, ctx_factory):
    bodies = {
        "kp": [{"time_tag": "2026-11-23T00:00:00", "Kp": 3.0}],
        "kp_1m": [{"time_tag": "2026-11-23T04:15:00", "kp_index": 5, "estimated_kp": 5.33}],
        "scales": {"0": {"G": {"Scale": "1"}, "S": {"Scale": "0"}, "R": {"Scale": "0"}}},
        "alerts": [],
        "wind_speed": [{"proton_speed": 650, "time_tag": "2026-11-23T04:15:00Z"}],
        "wind_mag": [{"bt": 12, "bz_gsm": -9, "time_tag": "2026-11-23T04:15:00Z"}],
        "flares": [],
    }
    for name, path in PRODUCTS.items():
        resp = httpx.Response(200, json=bodies[name]) if name in bodies else httpx.Response(502)
        respx.get(BASE + path).mock(return_value=resp)
    respx.get(BASE + AURORA).mock(return_value=httpx.Response(200, json={
        "Observation Time": "t", "coordinates": [[0, 65, 40]]}))
    async with httpx.AsyncClient() as http:
        await SwpcCollector({}).poll(ctx_factory(http))
    sw = read(tmp_path, "spaceweather")
    assert sw["kp_now"][1] == 5.33 and sw["g_now"] == 1
    assert sw["solar_wind"]["bz_nt"] == -9 and sw["scales"]["now"]["G"] == 1
    assert sw["errors"] and sw["errors"][0].startswith("f107")
    assert read(tmp_path, "aurora")["max"] == 40


@respx.mock
async def test_swpc_all_failed_raises(ctx_factory):
    for path in [*PRODUCTS.values(), AURORA]:
        respx.get(BASE + path).mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError):
            await SwpcCollector({}).poll(ctx_factory(http))


# ------------------------------------------------------------------ radiation


@respx.mock
async def test_radiation_poll(tmp_path, ctx_factory):
    from datetime import UTC, datetime, timedelta

    recent = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00Z")
    respx.get(BFS_URL).mock(return_value=httpx.Response(200, json={"features": [
        {"geometry": {"coordinates": [10.13, 47.64]}, "properties": {
            "id": "DEZ1874", "name": "Weitnau", "site_status": 1, "end_measure": recent,
            "value": 0.112}}]}))
    respx.get(SAFECAST_URL).mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http:
        batch = await RadiationCollector({}).poll(ctx_factory(http))
    assert [o.id for o in batch.observations] == ["rad:bfs:DEZ1874"]
    snap = read(tmp_path, "radiation")
    assert snap["networks"]["bfs"] == {"ok": True, "stations": 1}
    assert snap["networks"]["safecast"]["ok"] is False
    assert json.dumps(snap["stations"][0]["ts"]).startswith('"20')
