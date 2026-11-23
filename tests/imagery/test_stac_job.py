import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.imagery import mount_imagery
from osint.imagery import png
from osint.imagery.config import ImageryConfig
from osint.imagery.job import ImageryJob, ImageryProcessor, needs_work
from osint.imagery.stac import parse_scene, pick_after, pick_before, search_scenes
from osint.imagery.store import ImageryStore

from .util import scene_tiff

STAC = "https://stac.test/v1"
LAT, LON = 50.45466, 30.5238
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
T0 = NOW - timedelta(days=3)
EID = "01M7ZB4QHB327SQBV8XA2CQFSY"


def feature(sid, days_ago, cloud, *, href="https://cogs.test", coll="sentinel-2-l2a"):
    dt = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": sid, "collection": coll, "bbox": [29.7, 49.5, 31.0, 50.5],
            "properties": {"datetime": dt, "eo:cloud_cover": cloud, "proj:epsg": 32635},
            "assets": {"visual": {"href": f"{href}/{sid}/TCI.tif"},
                       "thumbnail": {"href": f"{href}/{sid}/thumb.jpg"}}}


def scenes(*specs, **kw):
    return [parse_scene(feature(*s, **kw)) for s in specs]


def test_parse_scene_and_readable():
    s = parse_scene(feature("A", 5, 3.2))
    assert (s.id, s.cloud_pct, s.epsg) == ("A", 3.2, 32635) and s.readable
    assert s.visual.endswith("A/TCI.tif") and s.thumbnail.endswith("thumb.jpg")
    assert not parse_scene(feature("B", 5, 1, href="s3://usgs-landsat")).readable
    assert parse_scene({"id": "x"}) is None
    land = parse_scene({"id": "L", "collection": "landsat-c2-l2", "bbox": [0, 0, 1, 1],
                        "properties": {"datetime": "2026-11-13T08:55:31.005181Z",
                                       "eo:cloud_cover": 100},
                        "assets": {k: {"href": "s3://b/x"} for k in ("red", "green", "blue")}})
    assert not land.readable  # requester-pays s3:// is skipped


def test_selection_before_after():
    ss = scenes(("b_far_clear", 40, 0.0), ("b_near_cloudy", 5, 25.0), ("b_near_ok", 4, 8.0),
                ("b_old", 80, 0.0), ("a1", 2, 5.0), ("a2", 1, 4.0), ("a_cloudy", 0.5, 29.0),
                ("unread", 6, 0.0))
    ss[-1].visual = "s3://x/TCI.tif"
    # T0 is 3 days ago: before = 40, 5, 4, 80, 6 days ago
    b = pick_before(ss, T0, lookback_days=60)
    assert b.id == "b_near_ok"  # cloud bucket 0 and nearest of those (4 days before event)
    # 'unread' (6 d ago, 0 %) is skipped: s3 only; 'b_old' beyond the 60-day window
    assert pick_before(ss, T0, lookback_days=60).id != "b_old"
    # after: a1/a2 at/after T0; same cloud bucket -> earliest wins
    a = pick_after(ss, T0, NOW)
    assert a.id == "a1"
    assert pick_after(ss, NOW + timedelta(days=1), NOW + timedelta(days=2)) is None
    assert pick_before([], T0) is None


@respx.mock
def test_search_request_and_filtering():
    route = respx.post(f"{STAC}/search").mock(return_value=httpx.Response(200, json={
        "features": [feature("ok", 4, 10.0), feature("cloudy", 4, 50.0), {"bad": 1}]}))
    with httpx.Client() as c:
        out = search_scenes(c, STAC, "sentinel-2-l2a", LAT, LON, T0 - timedelta(days=60), NOW,
                            30.0)
    assert [s.id for s in out] == ["ok"]
    body = json.loads(route.calls[0].request.content)
    assert body["collections"] == ["sentinel-2-l2a"]
    assert body["intersects"]["coordinates"] == [LON, LAT]
    assert body["query"]["eo:cloud_cover"]["lte"] == 30.0
    assert body["datetime"].endswith("2026-11-21T12:00:00Z")


def cfg(**kw):
    return ImageryConfig(stac_url=STAC, **kw)


def event(eid=EID, **kw):
    return {"id": eid, "lat": LAT, "lon": LON, "precision": "city", "status": "multi-source",
            "t_start": T0.isoformat(), "updated_at": NOW.isoformat(), **kw}


def mock_cog(sid, data):
    def handler(request: httpx.Request) -> httpx.Response:
        a, b = (int(x) for x in request.headers["Range"].removeprefix("bytes=").split("-"))
        return httpx.Response(206, content=data[a:b + 1])

    respx.get(f"https://cogs.test/{sid}/TCI.tif").mock(side_effect=handler)


@respx.mock
def test_process_full_pipeline_writes_crops_and_change_score(tmp_path):
    respx.post(f"{STAC}/search").mock(return_value=httpx.Response(200, json={"features": [
        feature("before1", 6, 2.0), feature("after1", 1, 3.0)]}))
    mock_cog("before1", scene_tiff(LAT, LON, pixels=lambda x, y: (50, 60, 70)))
    mock_cog("after1", scene_tiff(LAT, LON, pixels=lambda x, y: (150, 60, 70)))
    store = ImageryStore(tmp_path / "imagery.sqlite")
    proc = ImageryProcessor(cfg(), store, tmp_path)
    with httpx.Client() as c:
        proc.process(event(), c, NOW)
    rows = {r["role"]: r for r in store.get_assets(EID)}
    assert rows["before"]["status"] == rows["after"]["status"] == "ok"
    assert rows["before"]["scene_id"] == "before1" and rows["after"]["scene_id"] == "after1"
    assert rows["before"]["path"] == f"imagery/{EID}/before.png"
    w, h, data = png.decode_rgb((tmp_path / rows["after"]["path"]).read_bytes())
    assert (w, h) == (500, 500)
    assert data[(250 * 500 + 250) * 3:][:3] == bytes([150, 60, 70])
    assert data[:3] == bytes([0, 0, 0])  # outside the 80x80 px raster -> padded nodata
    assert rows["after"]["bounds"]["kind"] == "crop"
    assert rows["before"]["change_score"] == pytest.approx(100 / (3 * 255))
    assert rows["after"]["change_score"] == rows["before"]["change_score"]


@respx.mock
def test_awaiting_after_then_retry_and_idempotency(tmp_path):
    search = respx.post(f"{STAC}/search").mock(return_value=httpx.Response(200, json={
        "features": [feature("before1", 6, 2.0)]}))
    mock_cog("before1", scene_tiff(LAT, LON))
    store = ImageryStore(tmp_path / "imagery.sqlite")
    proc = ImageryProcessor(cfg(), store, tmp_path)
    with httpx.Client() as c:
        proc.process(event(), c, NOW)
        rows = {r["role"]: r for r in store.get_assets(EID)}
        assert rows["before"]["status"] == "ok" and rows["after"]["status"] == "awaiting_after"
        assert rows["after"]["change_score"] is None
        assert not needs_work(list(rows.values()), NOW + timedelta(hours=1), 3)
        assert needs_work(list(rows.values()), NOW + timedelta(hours=25), 3)
        # an after scene shows up a day later; before is not re-fetched
        search.mock(return_value=httpx.Response(200, json={"features": [
            feature("before1", 7, 2.0), feature("after1", 0.1, 1.0)]}))
        mock_cog("after1", scene_tiff(LAT, LON))
        proc.process(event(), c, NOW + timedelta(days=1))
    rows = {r["role"]: r for r in store.get_assets(EID)}
    assert rows["after"]["status"] == "ok" and rows["after"]["scene_id"] == "after1"
    assert rows["before"]["scene_id"] == "before1"
    assert rows["before"]["change_score"] is not None
    assert not needs_work(list(rows.values()), NOW + timedelta(days=9), 3)


@respx.mock
def test_preview_fallback_and_failure_recording(tmp_path):
    respx.post(f"{STAC}/search").mock(return_value=httpx.Response(200, json={"features": [
        feature("before1", 6, 2.0), feature("after1", 1, 3.0)]}))
    respx.get("https://cogs.test/before1/TCI.tif").mock(return_value=httpx.Response(404))
    respx.get("https://cogs.test/before1/thumb.jpg").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8jpegbytes",
                                    headers={"content-type": "image/jpeg"}))
    respx.get("https://cogs.test/after1/TCI.tif").mock(return_value=httpx.Response(500))
    respx.get("https://cogs.test/after1/thumb.jpg").mock(return_value=httpx.Response(503))
    store = ImageryStore(tmp_path / "imagery.sqlite")
    with httpx.Client() as c:
        ImageryProcessor(cfg(), store, tmp_path).process(event(), c, NOW)
    rows = {r["role"]: r for r in store.get_assets(EID)}
    b = rows["before"]
    assert b["status"] == "ok" and b["path"].endswith("before.jpg")
    assert "preview fallback" in b["error"] and b["bounds"]["kind"] == "preview"
    assert 0 <= b["bounds"]["marker"]["x"] <= 1
    assert (tmp_path / b["path"]).read_bytes().startswith(b"\xff\xd8")
    assert rows["after"]["status"] == "failed" and rows["after"]["attempts"] == 1
    assert "preview failed" in rows["after"]["error"]
    assert rows["before"]["change_score"] is None


@respx.mock
def test_stac_down_is_recorded(tmp_path):
    respx.post(f"{STAC}/search").mock(return_value=httpx.Response(502))
    store = ImageryStore(tmp_path / "imagery.sqlite")
    with httpx.Client() as c:
        ImageryProcessor(cfg(), store, tmp_path).process(event(), c, NOW)
    rows = store.get_assets(EID)
    assert {r["status"] for r in rows} == {"failed"} and "STAC search failed" in rows[0]["error"]


class FakeEvents:
    def __init__(self, events):
        self.events, self.calls = events, []

    def list_events(self, **kw):
        self.calls.append(kw)
        return self.events


def test_needs_work_rules():
    ok = {"status": "ok", "attempts": 0, "next_try_at": None}
    assert needs_work([], NOW, 3)
    assert not needs_work([{**ok, "role": "before"}, {**ok, "role": "after"}], NOW, 3)
    failed = {"status": "failed", "attempts": 3, "next_try_at": (NOW - timedelta(hours=1)).isoformat()}
    assert not needs_work([failed], NOW, 3)
    assert needs_work([{**failed, "attempts": 1}], NOW, 3)
    assert not needs_work([{"status": "no_scene", "attempts": 0, "next_try_at": None}], NOW, 3)


async def test_job_selection_cap_and_idempotent_run(tmp_path):
    evs = [event(f"EV{i:03d}") for i in range(7)]
    evs.append(event("EVREGION", precision="country"))
    evs.append(event("EVNOLOC", lat=None))
    evs.append(event("../evil"))
    store = ImageryStore(tmp_path / "imagery.sqlite")
    fake = FakeEvents(evs)
    done, active, peak = [], 0, 0
    job = ImageryJob(fake, store, cfg(max_per_run=5, concurrency=3), tmp_path,
                     client_factory=lambda: httpx.Client())

    def fake_process(ev, client, now=None):
        nonlocal active, peak
        done.append(ev["id"])
        store.upsert(ev["id"], "before", status="no_scene", now=NOW)
        store.upsert(ev["id"], "after", status="ok", now=NOW)

    job.processor.process = fake_process
    ran = await job.run_once()
    assert ran == [f"EV{i:03d}" for i in range(5)]
    kw = fake.calls[0]
    assert kw["status"] == "multi-source,sensor-corroborated,official" and kw["since"]
    assert await job.run_once() == ["EV005", "EV006"]  # processed events are not repicked
    assert await job.run_once() == []
    assert sorted(done) == sorted(f"EV{i:03d}" for i in range(7))


async def test_job_records_crashes_and_respects_concurrency(tmp_path):
    store = ImageryStore(tmp_path / "imagery.sqlite")
    job = ImageryJob(FakeEvents([event(f"EV{i:03d}") for i in range(6)]), store,
                     cfg(max_per_run=6, concurrency=3), tmp_path,
                     client_factory=lambda: httpx.Client())
    import threading
    import time
    lock, state = threading.Lock(), {"cur": 0, "peak": 0}

    def fake_process(ev, client, now=None):
        with lock:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        time.sleep(0.05)
        with lock:
            state["cur"] -= 1
        if ev["id"] == "EV000":
            raise RuntimeError("boom")
        store.upsert(ev["id"], "before", status="ok", now=NOW)
        store.upsert(ev["id"], "after", status="ok", now=NOW)

    job.processor.process = fake_process
    await asyncio.wait_for(job.run_once(), 10)
    assert state["peak"] <= 3
    crashed = {r["role"]: r for r in store.get_assets("EV000")}
    assert crashed["after"]["status"] == "failed" and crashed["after"]["attempts"] == 1


# ---------------------------------------------------------------- API


def api(tmp_path):
    rt = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    app = FastAPI()
    mount_imagery(app, rt)
    from osint.imagery.job import get_store
    return TestClient(app), get_store(rt)


def test_api_assets_files_and_attribution(tmp_path):
    client, store = api(tmp_path)
    d = tmp_path / "imagery" / EID
    d.mkdir(parents=True)
    (d / "before.png").write_bytes(png.encode_rgb(1, 1, b"abc"))
    (d / "after.jpg").write_bytes(b"\xff\xd8jpg")
    store.upsert(EID, "before", status="ok", now=NOW, collection="sentinel-2-l2a",
                 scene_id="S", acquired_at=datetime(2026, 11, 10, tzinfo=UTC), cloud_pct=2.0,
                 path=f"imagery/{EID}/before.png", bounds={"kind": "crop"}, change_score=0.1)
    store.upsert(EID, "after", status="ok", now=NOW, collection="landsat-c2-l2",
                 scene_id="L", acquired_at=datetime(2026, 11, 18, tzinfo=UTC), cloud_pct=9.0,
                 path=f"imagery/{EID}/after.jpg", bounds={"kind": "preview"})
    j = client.get(f"/api/imagery/{EID}").json()
    by = {a["role"]: a for a in j["assets"]}
    # ?v= (the row's update time) busts a cached copy when a crop is redone
    assert by["before"]["url"] == f"/api/imagery/{EID}/before.png?v=20261121120000"
    assert by["after"]["url"] == f"/api/imagery/{EID}/after.jpg?v=20261121120000"
    assert by["before"]["attribution"] == "Contains modified Copernicus Sentinel data 2026"
    assert by["after"]["attribution"] == "Landsat imagery courtesy of USGS"
    assert by["before"]["change_score"] == 0.1 and by["before"]["cloud_pct"] == 2.0
    assert len(j["attributions"]) == 2
    r = client.get(by["before"]["url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert client.get(f"/api/imagery/{EID}/after.jpg").headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/imagery/{EID}/before.jpg").status_code == 404  # wrong extension
    assert client.get(f"/api/imagery/{EID}").json()["event_id"] == EID
    assert client.get("/api/imagery/NOSUCHEVENT").json()["assets"] == []


def test_api_path_safety(tmp_path):
    client, store = api(tmp_path)
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"top secret")
    store.upsert(EID, "before", status="ok", now=NOW, path="imagery/../secret.png")
    store.upsert("EVOTHER", "before", status="ok", now=NOW, path="../../etc/passwd.png")
    assert client.get(f"/api/imagery/{EID}/before.png").status_code == 404  # escapes the root
    assert client.get("/api/imagery/EVOTHER/before.png").status_code == 404
    for bad in ("..%2Fsecret", "%2e%2e", "a b", "x" * 80, "ev.id", "ev%00"):
        assert client.get(f"/api/imagery/{bad}").status_code == 404
        assert client.get(f"/api/imagery/{bad}/before.png").status_code == 404
    for name in ("..%2Fbefore.png", "before.gif", "before.png.exe", "%2e%2e%2fsecret.png",
                 "other.png"):
        assert client.get(f"/api/imagery/{EID}/{name}").status_code == 404
