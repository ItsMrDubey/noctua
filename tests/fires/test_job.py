import json
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.m1 import mount_m1
from osint.config import Settings
from osint.core.models import Observation
from osint.fires import frontline, reference
from osint.fires import job as fire_job
from osint.fires.config import FireScoreConfig
from osint.fires.job import STATUS_KEY, FireScoreJob
from osint.fires.store import FireScoreStore, get_score_store
from osint.runtime import Runtime
from osint.store.lake import Lake, observation_row
from osint.store.sqlite import SqliteStore, iso
from tests.fires.test_frontline import countries, payload
from tests.fires.test_reference import GVP, xlsx

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
DS_URL = "https://ds.test/last"
CFG = FireScoreConfig.from_dict({
    "frontline": {"url": DS_URL},
    "reference": {"volcano_url": "https://gvp.test/wfs", "flare_url": "https://eog.test/f.xlsx"}})


def fire(i, ts, lat, lon, frp=10.0, dn="N"):
    return Observation(id=f"fire:{i}", type="fire", ts=ts, lat=lat, lon=lon, source="firms:t",
                       value=frp, attrs={"confidence": "Nominal", "daynight": dn})


def event(eid, lat, lon, *, origin="news", category="armed_conflict", precision="point",
          updated=NOW - timedelta(hours=2)):
    t = iso(updated)
    return {"id": eid, "category": category, "subcategory": None, "cameo_root": None,
            "title": f"Event {eid}", "summary": "", "actors": [], "lat": lat, "lon": lon,
            "precision": precision, "place_name": None, "country": None,
            "geo_confidence": 0.9, "h3_3": None, "h3_5": None, "h3_7": None, "t_start": t,
            "t_end": t, "status": "reported", "confidence": 0.5, "version": 1,
            "mention_count": 1, "source_diversity": 1, "gdelt_ids": [], "first_seen": t,
            "updated_at": t, "cluster_state": {}, "origin": origin}


@pytest.fixture
def env(tmp_path):
    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "osint.sqlite")
    scores = FireScoreStore(tmp_path / "fire_scores.sqlite")
    ref = tmp_path / "reference"
    ref.mkdir()
    countries(ref)
    clock = {"now": NOW}
    j = FireScoreJob(lake, store, scores, CFG, ref_dir=ref, gazetteer_dir=tmp_path / "gz",
                     http=None, pois_loader=list, clock=lambda: clock["now"])
    yield j, lake, store, scores, clock
    scores.close()
    store.close()
    lake.close()


def seed_frontline(ref):
    frontline.save(frontline.build(payload(), ref), ref)


async def test_run_once_scores_window_incrementally(env):
    j, lake, store, scores, clock = env
    seed_frontline(j.ref_dir)
    lake.write("observations", [observation_row(o) for o in [
        fire(1, NOW - timedelta(hours=1), 48.0, 35.98),        # ~1.5 km west of the front
        fire(2, NOW - timedelta(hours=2), 48.0, 32.0, dn="D"),  # deep in held territory
        fire(3, NOW - timedelta(hours=50), 48.0, 35.98),       # outside the 36 h window
    ]])
    store.save_events([
        event("near", 48.0, 35.97),
        event("hist", 48.0, 35.99, origin="historical", updated=NOW - timedelta(days=400)),
        event("country", 48.0, 35.97, precision="country"),
        event("eco", 48.0, 35.97, category="economic")])
    assert await j.run_once() == 2
    r1, r2 = scores.get("fire:1"), scores.get("fire:2")
    assert scores.get("fire:3") is None
    assert r1["score"] >= 70 and r1["reasons"][0] == "near_frontline"
    assert "conflict_news_5km" in r1["reasons"] and "ucdp_weak" in r1["reasons"]
    assert r1["features"]["news_event"]["id"] == "near"
    assert r2["score"] < 20 and r2["ts"] == "2026-11-23T10:00:00+00:00"
    st = scores.get_meta(STATUS_KEY)
    assert st["scored"] == 2 and st["war_likely"] == 1 and st["full_rescore"] is True
    assert st["context"]["news"] == 1 and st["context"]["ucdp"] == 1
    assert st["context"]["frontline"]["clipped_to_ukraine"] is True

    # next pass: only the new hotspot is scored
    clock["now"] = NOW + timedelta(minutes=5)
    lake.write("observations", [observation_row(fire(4, NOW + timedelta(minutes=1), 10, 10))])
    assert await j.run_once() == 1
    # a config change rescores the window; so does the periodic full rescore
    j.cfg = FireScoreConfig.from_dict({"weights": {"night": 1}})
    assert await j.run_once() == 3
    clock["now"] = NOW + timedelta(hours=4)
    assert await j.run_once() == 3


async def test_recurrence_from_lake_history(env):
    j, lake, _, scores, _ = env
    obs = []
    day0 = NOW.replace(hour=1)
    for d in range(1, 9):  # a flare-like source seen on 8 past days, jittering across cells
        obs.append(fire(f"h{d}", day0 - timedelta(days=d), 52.55 + (d % 2) * 0.004, 39.6,
                        frp=3.0))
    obs.append(fire("other", day0 - timedelta(days=2), 10.0, 10.0))
    obs.append(fire("today", NOW - timedelta(hours=1), 52.5501, 39.6001, frp=4.0))
    obs.append(fire("strike", NOW - timedelta(hours=1), 52.5502, 39.6002, frp=200.0))
    lake.write("observations", [observation_row(o) for o in obs])
    rec = j.recurrence(NOW)
    assert rec.history_days == 8
    days, median = rec.lookup(52.5501, 39.6001)
    assert days == 8 and median == pytest.approx(3.0)
    assert rec.lookup(10.0, 10.0) == (0, None)  # one day only: not kept
    await j.run_once()
    assert "static_site" in scores.get("fire:today")["reasons"]
    assert "static_anomaly" in scores.get("fire:strike")["reasons"]


async def test_refresh_reference_downloads_and_backs_off(env):
    j, *_ = env
    with respx.mock:
        ds = respx.get(DS_URL).mock(return_value=httpx.Response(200, json=payload()))
        respx.get(CFG.reference.volcano_url).mock(return_value=httpx.Response(200, json=GVP))
        fl = respx.get(CFG.reference.flare_url).mock(return_value=httpx.Response(503))
        async with httpx.AsyncClient() as http:
            j.http = http
            await j.refresh_reference(NOW)
            assert frontline.load(j.ref_dir)["source_id"] == 42
            assert len(reference.load_volcanoes(j.ref_dir)) == 3
            assert j.fetch_errors == {reference.FLARE_FILE: "EOG flares: HTTP 503"}
            # fresh caches and the failed download's back-off: nothing is fetched again
            await j.refresh_reference(NOW + timedelta(minutes=5))
            assert ds.call_count == 1 and fl.call_count == 1
            fl.mock(return_value=httpx.Response(200, content=xlsx()))
            await j.refresh_reference(NOW + timedelta(seconds=fire_job.RETRY_S + 60))
            assert fl.call_count == 2 and j.fetch_errors == {}
            assert len(reference.load_flares(j.ref_dir)) == 2


async def test_frontline_refresh_failure_keeps_cache(env):
    j, *_ = env
    seed_frontline(j.ref_dir)
    old = frontline.load(j.ref_dir)
    stale = (NOW - timedelta(hours=7)).timestamp()
    os.utime(j.ref_dir / frontline.CACHE_NAME, (stale, stale))
    with respx.mock:
        respx.get(DS_URL).mock(return_value=httpx.Response(200, json={"map": {"features": []}}))
        async with httpx.AsyncClient() as http:
            j.http = http
            await j.refresh_reference(NOW)
    assert frontline.CACHE_NAME in j.fetch_errors
    assert frontline.load(j.ref_dir) == old


def test_store_between_prune_and_stats(tmp_path):
    s = FireScoreStore(tmp_path / "s.sqlite")
    rows = [{"id": f"fire:{i}", "ts": NOW - timedelta(hours=i), "lat": 1.0, "lon": 2.0,
             "score": 20 * i, "reasons": ["night"] if i else [], "features": {"a": i}}
            for i in range(4)]
    assert s.save(rows, "fp1", NOW) == 4
    got = s.between(NOW - timedelta(hours=2), NOW)
    assert got == {"fire:0": (0, ""), "fire:1": (20, "night"), "fire:2": (40, "night")}
    assert s.fingerprints(NOW - timedelta(hours=1), NOW) == {"fire:0": "fp1", "fire:1": "fp1"}
    assert s.stats(NOW - timedelta(days=1), NOW, 40) == {
        "scored": 4, "war_likely": 2, "threshold": 40}
    assert s.get("fire:3")["features"] == {"a": 3} and s.get("nope") is None
    assert s.prune(NOW - timedelta(hours=2, minutes=30)) == 1
    s.close()


# ------------------------------------------------------------------ API


@pytest.fixture
def client(tmp_path):
    rt = Runtime(Settings(_env_file=None, data_dir=tmp_path), [])
    now = datetime.now(UTC)
    rt.lake.write("observations", [observation_row(o) for o in [
        fire(1, now - timedelta(hours=1), 48.0, 36.0), fire(2, now - timedelta(hours=2), 5, 6)]])
    get_score_store(rt).save([{"id": "fire:1", "ts": now - timedelta(hours=1), "lat": 48.0,
                               "lon": 36.0, "score": 88, "reasons": ["near_frontline", "night"],
                               "features": {"frontline_km": 1.2}}], "fp")
    app = FastAPI()
    mount_m1(app, rt)
    yield TestClient(app), rt
    rt.fire_score_store.close()
    rt.store.close()


def test_fires_layer_carries_scores(client):
    c, _ = client
    items = {i["id"]: i for i in c.get("/api/layers/fires").json()["items"]}
    assert items["fire:1"]["conflict_score"] == 88
    assert items["fire:1"]["conflict_reasons"] == ["near_frontline", "night"]
    assert items["fire:2"]["conflict_score"] is None and items["fire:2"]["conflict_reasons"] == []


def test_fire_score_detail_and_status(client):
    c, rt = client
    d = c.get("/api/fires/score/fire:1").json()
    assert d["score"] == 88 and d["features"] == {"frontline_km": 1.2}
    assert c.get("/api/fires/score/fire:2").status_code == 404
    assert c.get("/api/fires/score-status").json() == {"last_run_at": None}
    get_score_store(rt).set_meta(STATUS_KEY, {"last_run_at": "x", "scored": 1})
    assert c.get("/api/fires/score-status").json()["scored"] == 1


def test_frontline_endpoint_serves_the_cache(client):
    c, rt = client
    assert c.get("/api/reference/frontline").json()["features"] == []
    ref = rt.settings.data_dir / "reference"
    ref.mkdir(exist_ok=True)
    countries(ref)
    frontline.save(frontline.build(payload(), ref), ref)
    gj = c.get("/api/reference/frontline").json()
    assert gj["features"][0]["geometry"]["type"] == "MultiLineString"
    assert gj["features"][0]["properties"]["source_id"] == 42
    assert json.loads(json.dumps(gj))  # plain JSON
