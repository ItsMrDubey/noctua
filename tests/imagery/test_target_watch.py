"""Crop centring on the impact site / FIRMS hull, re-crop after re-location, and the real
'why is there no after image' state (latest cloudy pass, next check, projected pass)."""
import json
from datetime import timedelta

import httpx
import respx

from osint.api.imagery import watch_status
from osint.imagery.config import ImageryConfig
from osint.imagery.job import ImageryProcessor, covers, needs_work, target_point
from osint.imagery.store import ImageryStore

from .test_stac_job import EID, LAT, LON, NOW, STAC, T0, cfg, event, feature, mock_cog
from .util import scene_tiff

HULL = {"kind": "firms_hull", "basis": "FIRMS hull", "geometry": {"type": "Polygon", "coordinates": [[
    [LON + 0.001, LAT + 0.001], [LON + 0.003, LAT + 0.001], [LON + 0.003, LAT + 0.003],
    [LON + 0.001, LAT + 0.003], [LON + 0.001, LAT + 0.001]]]}}


def test_target_point_priority():
    ev = {"lat": LAT, "lon": LON}
    assert target_point(ev) == (LAT, LON, "event")
    lat, lon, basis = target_point(ev, {"best": None, "areas": [HULL]})
    assert basis == "firms_hull" and abs(lat - (LAT + 0.002)) < 1e-9 and abs(lon - (LON + 0.002)) < 1e-9
    assert target_point(ev, {"best": {"lat": 1.0, "lon": 2.0}, "areas": [HULL]}) == (1.0, 2.0,
                                                                                     "impact_site")


def test_covers_and_needs_work_on_relocation():
    b = {"kind": "crop", "west": 30.0, "east": 30.1, "south": 50.0, "north": 50.1}
    assert covers(b, 50.05, 30.05) and not covers(b, 50.2, 30.05)
    assert covers({"kind": "preview"}, 0, 0) and covers(None, 0, 0)
    ok = {"role": "before", "status": "ok", "attempts": 0, "next_try_at": None, "bounds": b}
    assert not needs_work([ok], NOW, 3, (50.05, 30.05))
    assert needs_work([ok], NOW, 3, (50.2, 30.05))


@respx.mock
def test_crop_centred_on_hull_and_redone_when_moved(tmp_path):
    respx.post(f"{STAC}/search").mock(return_value=httpx.Response(200, json={
        "features": [feature("before1", 6, 2.0)]}))
    mock_cog("before1", scene_tiff(LAT, LON))
    store = ImageryStore(tmp_path / "imagery.sqlite")
    proc = ImageryProcessor(cfg(), store, tmp_path)
    with httpx.Client() as c:
        proc.process(event(), c, NOW, impact={"best": None, "areas": [HULL]})
    before = {r["role"]: r for r in store.get_assets(EID)}["before"]
    assert before["status"] == "ok" and before["bounds"]["target"]["basis"] == "firms_hull"
    assert covers(before["bounds"], LAT + 0.002, LON + 0.002)
    # the event is re-located 20 km away: the stale crop is redone (before, not just after)
    calls = []
    proc._save = lambda eid, role, *a, **kw: calls.append((role, kw["basis"]))
    far = {"best": {"lat": LAT + 0.2, "lon": LON, "name": "Site"}, "areas": []}
    with httpx.Client() as c:
        proc.process(event(), c, NOW + timedelta(hours=1), impact=far)
    assert ("before", "impact_site") in calls


@respx.mock
def test_awaiting_after_records_latest_cloudy_pass(tmp_path):
    def search(request):
        body = json.loads(request.content)
        cloudy = body["query"]["eo:cloud_cover"]["lte"] >= 100
        feats = [feature("before1", 6, 2.0)] + ([feature("after_cloudy", 1, 87.0)] if cloudy else [])
        return httpx.Response(200, json={"features": feats})

    respx.post(f"{STAC}/search").mock(side_effect=search)
    mock_cog("before1", scene_tiff(LAT, LON))
    store = ImageryStore(tmp_path / "imagery.sqlite")
    with httpx.Client() as c:
        ImageryProcessor(cfg(), store, tmp_path).process(event(), c, NOW)
    after = {r["role"]: r for r in store.get_assets(EID)}["after"]
    assert after["status"] == "awaiting_after" and after["scene_id"] == "after_cloudy"
    assert after["cloud_pct"] == 87.0 and "cloud 87% > 30% limit" in after["error"]
    assert after["next_try_at"] is not None


def test_watch_status_projects_next_pass_and_expiry():
    c = ImageryConfig()
    rows = [{"role": "before", "status": "ok", "collection": "sentinel-2-l2a",
             "acquired_at": (NOW - timedelta(days=12)).isoformat()},
            {"role": "after", "status": "awaiting_after", "collection": None, "acquired_at": None}]
    w = watch_status(rows, T0.isoformat(), c, now=NOW)
    assert w["state"] == "awaiting" and w["max_cloud_pct"] == 30.0
    assert w["next_pass_est"].startswith((NOW + timedelta(days=3)).date().isoformat())
    assert "estimate" in w["next_pass_basis"]
    assert watch_status(rows, T0.isoformat(), c, now=NOW + timedelta(days=9))["state"] == "expired"
    assert watch_status([], T0.isoformat(), c, now=NOW)["state"] == "not_scheduled"
    rows[1].update(status="ok", acquired_at=NOW.isoformat(), collection="sentinel-2-l2a")
    w = watch_status(rows, T0.isoformat(), c, now=NOW)
    assert w["state"] == "acquired" and w["next_pass_est"] is None
