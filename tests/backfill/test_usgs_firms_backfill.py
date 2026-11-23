import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from osint.backfill import firms, usgs
from osint.backfill.common import Polite, Window
from osint.core.models import Observation

W = Window(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC))


def feature(i: int, ts: datetime) -> dict:
    return {"id": f"us{i}", "geometry": {"coordinates": [10.0, 20.0, 5.0]},
            "properties": {"time": int(ts.timestamp() * 1000), "mag": 3.1,
                           "updated": 1, "type": "earthquake", "place": "x"}}


def _starttime(req: httpx.Request) -> datetime:
    return datetime.fromisoformat(req.url.params["starttime"]).replace(tzinfo=UTC)


async def test_usgs_splits_chunks_that_hit_the_cap():
    def handler(req: httpx.Request) -> httpx.Response:
        start = _starttime(req)
        end = datetime.fromisoformat(req.url.params["endtime"]).replace(tzinfo=UTC)
        if end - start > timedelta(days=1):  # whole window: over the 3-feature cap
            return httpx.Response(200, json={"features": [feature(i, start) for i in range(3)]})
        return httpx.Response(200, json={"features": [feature(int(start.day), start)]})

    with respx.mock:
        route = respx.get(usgs.QUERY_URL).mock(side_effect=handler)
        async with httpx.AsyncClient() as http:
            feats = await usgs.fetch_chunk(http, W, min_magnitude=2.5, polite=Polite(1, 0),
                                           max_per_query=3)
    assert route.call_count == 3  # whole window, then two halves
    assert [f["id"] for f in feats] == ["us1", "us2"]
    assert route.calls[0].request.url.params["minmagnitude"] == "2.5"


async def test_usgs_splits_on_http_400_search_limit():
    def handler(req: httpx.Request) -> httpx.Response:
        start = _starttime(req)
        end = datetime.fromisoformat(req.url.params["endtime"]).replace(tzinfo=UTC)
        if end - start > timedelta(days=1):
            return httpx.Response(400, text="Error 400: query exceeds search limit of 20000")
        return httpx.Response(200, json={"features": [feature(start.day, start)]})

    with respx.mock:
        respx.get(usgs.QUERY_URL).mock(side_effect=handler)
        async with httpx.AsyncClient() as http:
            feats = await usgs.fetch_chunk(http, W, min_magnitude=2.5, polite=Polite(1, 0))
    assert len(feats) == 2


async def test_usgs_run_writes_new_observations_and_resumes(make_ctx):
    with respx.mock:
        route = respx.get(usgs.QUERY_URL).mock(return_value=httpx.Response(
            200, json={"features": [feature(1, W.start), feature(2, W.start),
                                    {"id": "bad", "properties": {}}]}))
        ctx = make_ctx("usgs", {"min_interval_s": 0})
        s = await usgs.run(ctx, W)
        assert s.rows == {"observations": 2} and s.units_done == 1
        s2 = await usgs.run(make_ctx("usgs", {"min_interval_s": 0}), W)
        assert s2.units_skipped == 1 and route.call_count == 1
        # with the checkpoint cleared, existing (id, version) pairs are not rewritten
        ctx.store.set_cursor("backfill:usgs", {})
        s3 = await usgs.run(make_ctx("usgs", {"min_interval_s": 0}), W)
        assert s3.rows == {}
    assert ctx.lake.query("SELECT count(*) FROM observations")[0][0] == 2


# ------------------------------------------------------------------------- FIRMS

CSV = ("latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,"
       "confidence,version,bright_ti5,frp,daynight\n"
       "10.0,20.0,330,0.4,0.4,2026-01-01,0130,N,VIIRS,n,2,290,12.5,D\n"
       "10.1,20.1,330,0.4,0.4,2026-01-01,0130,N,VIIRS,l,2,290,40.0,D\n"
       "10.2,20.2,330,0.4,0.4,2026-01-01,0130,N,VIIRS,h,2,290,2.0,D\n")


def test_firms_plan_uses_sp_then_nrt():
    w = Window(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 25, 12, tzinfo=UTC))
    reqs = firms.plan(w, nrt_days=6, chunk_days=5)
    sp = [(p, c.start.day, (c.end - c.start).days) for p, c in reqs if p.endswith("_SP")]
    nrt = [(p, c.start.day) for p, c in reqs if p.endswith("_NRT")]
    # day-aligned end is Jan 26, so NRT covers the last 6 days from Jan 20; the API takes at
    # most 5 days per request
    assert sp == [("MODIS_SP", 1, 5), ("VIIRS_SNPP_SP", 1, 5), ("MODIS_SP", 6, 5),
                  ("VIIRS_SNPP_SP", 6, 5), ("MODIS_SP", 11, 5), ("VIIRS_SNPP_SP", 11, 5),
                  ("MODIS_SP", 16, 4), ("VIIRS_SNPP_SP", 16, 4)]
    assert nrt == [("MODIS_NRT", 20), ("VIIRS_SNPP_NRT", 20), ("MODIS_NRT", 25),
                   ("VIIRS_SNPP_NRT", 25)]
    too_long = firms.plan(w, nrt_days=6, chunk_days=10)[0][1]
    with pytest.raises(ValueError, match="at most 5 days"):
        firms.area_url("K", "MODIS_SP", too_long)
    assert firms.area_url("K", "MODIS_SP", reqs[0][1]) == \
        "https://firms.modaps.eosdis.nasa.gov/api/area/csv/K/MODIS_SP/world/5/2026-01-01"


def test_firms_history_filter():
    def obs(conf: str, frp: float | None) -> Observation:
        return Observation(id="f", type="fire", ts=W.start, lat=0, lon=0, source="s",
                           value=frp, attrs={"confidence": conf})
    assert firms.keep(obs("Nominal", 6), "nominal", 5.0)
    assert not firms.keep(obs("Low", 50), "nominal", 5.0)
    assert not firms.keep(obs("High (90%)", 4.9), "nominal", 5.0)
    assert firms.keep(obs("Nominal (40%)", None), "low", None)
    assert not firms.keep(obs("Nominal (40%)", 10), "high", None)


async def test_firms_skips_without_key(make_ctx):
    s = await firms.run(make_ctx("firms"), W)
    assert s.state == "skipped" and "FIRMS_MAP_KEY" in s.message


async def test_firms_run_filters_rate_limits_and_redacts(make_ctx, caplog):
    key = "SECRETKEY42"
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if "VIIRS" in req.url.path:
            return httpx.Response(200, text=CSV)
        return httpx.Response(200, text=f"Invalid MAP_KEY {key}.")

    ctx = make_ctx("firms", {"nrt_days": 0, "sp_products": ["MODIS_SP", "VIIRS_SNPP_SP"],
                             "chunk_days": 1}, firms_map_key=key)
    with respx.mock, caplog.at_level(logging.INFO):
        respx.get(url__startswith=firms.BASE_URL).mock(side_effect=handler)
        s = await firms.run(ctx, Window(W.start, W.start + timedelta(days=1)))
    assert len(calls) == 4  # 2 day-aligned chunks x 2 products
    assert s.rows == {"observations": 1}  # n + 12.5 MW kept; low conf and 2 MW dropped
    assert s.errors and all(key not in e for e in s.errors)
    assert all(key not in r.getMessage() for r in caplog.records
               if r.name.startswith("osint"))
    st = ctx.store.get_cursor("backfill:firms")
    assert key not in str(st)
