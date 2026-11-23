import asyncio
import itertools
from datetime import UTC, datetime

import httpx

from osint.weather.ir import (
    GIBS,
    LAT_LIMIT,
    SOURCES,
    IRFrames,
    candidate_slots,
    floor_slot,
    iso,
    parse_wms_default_time,
    probe_latest,
    sector_of,
    sectors,
)

NOW = datetime(2026, 11, 23, 9, 52, 14, tzinfo=UTC)


def test_sectors_meet_at_midpoints_and_tile_the_globe():
    secs = sectors([s.lon for s in SOURCES])
    by = {s.id: p for s, p in zip(SOURCES, secs)}
    assert by["goes_west"] == [(-178.25, -106.2)]
    assert by["goes_east"] == [(-106.2, -37.6)]
    assert by["mtg"] == [(-37.6, 22.75)]
    assert by["iodc"] == [(22.75, 93.1)]
    # Himawari's sector crosses the antimeridian: split into two pieces
    assert by["himawari"] == [(93.1, 180.0), (-180.0, -178.25)]
    pieces = sorted(p for ps in secs for p in ps)
    assert pieces[0][0] == -180.0 and pieces[-1][1] == 180.0
    assert all(a[1] == b[0] for a, b in itertools.pairwise(pieces))  # no gaps, no overlap
    assert sum(e - w for w, e in pieces) == 360.0


def test_sectors_order_independent_and_degenerate_cases():
    assert sectors([]) == []
    assert sectors([10.0]) == [[(-180.0, 180.0)]]
    a = sectors([140.7, -75.2, 0.0])
    b = sectors([0.0, 140.7, -75.2])
    assert a[0] == b[1] and a[1] == b[2] and a[2] == b[0]
    # a satellite exactly opposite another: boundaries at +/-90
    assert sectors([0.0, 180.0]) == [[(-90.0, 90.0)], [(90.0, 180.0), (-180.0, -90.0)]]


def test_each_longitude_belongs_to_the_nearest_satellite():
    data = {"sources": [{"id": s.id, "sector": p} for s, p in zip(SOURCES, sectors([s.lon for s in SOURCES]))]}
    assert sector_of(10, data) == "mtg"          # central Europe / Africa
    assert sector_of(35, data) == "iodc"         # Middle East, eastern Ukraine
    assert sector_of(77, data) == "iodc"         # India
    assert sector_of(-74, data) == "goes_east"
    assert sector_of(-122, data) == "goes_west"
    assert sector_of(179.9, data) == "himawari"
    assert sector_of(-179, data) == "himawari"
    assert sector_of(-178, data) == "goes_west"


def test_slots_floor_and_step_back():
    assert floor_slot(NOW, 10) == datetime(2026, 11, 23, 9, 50, tzinfo=UTC)
    assert floor_slot(NOW, 15) == datetime(2026, 11, 23, 9, 45, tzinfo=UTC)
    s = candidate_slots(NOW, 10, min_lag_min=10, steps=4)
    assert [iso(t) for t in s] == ["2026-11-23T09:40:00Z", "2026-11-23T09:30:00Z",
                                   "2026-11-23T09:20:00Z", "2026-11-23T09:10:00Z"]
    # across midnight
    s2 = candidate_slots(datetime(2026, 11, 23, 0, 5, tzinfo=UTC), 10, min_lag_min=10, steps=2)
    assert [iso(t) for t in s2] == ["2026-11-22T23:50:00Z", "2026-11-22T23:40:00Z"]


def test_probe_returns_newest_existing_slot():
    slots = candidate_slots(NOW, 10, steps=6)
    have = {"2026-11-23T09:20:00Z", "2026-11-23T09:00:00Z"}

    async def exists(t):
        return iso(t) in have
    t, n = asyncio.run(probe_latest(exists, slots))
    assert iso(t) == "2026-11-23T09:20:00Z" and n == 3
    t, n = asyncio.run(probe_latest(lambda t: asyncio.sleep(0, False), slots))
    assert t is None and n == 6


def test_parse_wms_time_default():
    xml = ('<Layer><Name>mtg_fd:ir105_hrfi</Name><Dimension name="time" default="2026-11-23T09:40:00Z" '
           'units="ISO8601" nearestValue="1">2024-09-23T00:00:00.000Z/2026-11-23T09:40:00.000Z/PT10M</Dimension></Layer>')
    assert parse_wms_default_time(xml) == datetime(2026, 11, 23, 9, 40, tzinfo=UTC)
    assert parse_wms_default_time("<Layer/>") is None


def _transport(gibs_have: dict[str, str], wms: dict[str, str | None], calls: list):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        u = str(req.url)
        if u.startswith(GIBS):
            layer = u[len(GIBS) + 1:].split("/")[0]
            ts = u.split("/default/")[1].split("/")[0]
            if layer == "BROKEN":
                return httpx.Response(500)
            return httpx.Response(200 if gibs_have.get(layer) and ts <= gibs_have[layer] else 404)
        for layer, t in wms.items():
            ws, name = layer.split(":")
            if f"/{ws}/{name}/ows" in u:
                if t is None:
                    return httpx.Response(503)
                return httpx.Response(200, text=f'<Dimension name="time" default="{t}" units="ISO8601">x</Dimension>')
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_frames_probe_each_source_with_fallback_and_cache():
    calls: list = []
    gibs = {"GOES-West_ABI_Band13_Clean_Infrared": "2026-11-23T09:30:00Z",
            "GOES-East_ABI_Band13_Clean_Infrared": "2026-11-23T09:20:00Z",
            "Himawari_AHI_Band13_Clean_Infrared": "2026-11-23T09:00:00Z"}
    wms = {"mtg_fd:ir105_hrfi": None, "msg_fes:ir108": "2026-11-23T09:30:00Z",
           "msg_iodc:ir108": "2026-11-23T09:30:00Z"}
    t = [0.0]
    svc = IRFrames(httpx.AsyncClient(transport=_transport(gibs, wms, calls)), clock=lambda: t[0], now=lambda: NOW)
    out = asyncio.run(svc.frames())
    by = {s["id"]: s for s in out["sources"]}
    # newest slot present, but the one before it is complete too: the settled frame is used
    assert by["goes_west"]["time"] == "2026-11-23T09:20:00Z"
    assert by["goes_east"]["time"] == "2026-11-23T09:10:00Z"
    assert by["himawari"]["time"] == "2026-11-23T08:50:00Z" and by["himawari"]["age_min"] == 62
    # MTG capabilities failed -> MSG 0 deg serves the same sector
    assert by["mtg"]["layer"] == "msg_fes:ir108" and by["mtg"]["fallback"] and by["mtg"]["sector"] == [[-37.6, 22.75]]
    assert by["iodc"]["time"] == "2026-11-23T09:30:00Z" and by["iodc"]["decode"] == "msg"
    assert out["lat_limit"] == LAT_LIMIT
    n = len(calls)
    asyncio.run(svc.frames())
    assert len(calls) == n  # cached
    t[0] = 10_000
    asyncio.run(svc.frames())
    assert len(calls) > n


def test_upstream_error_keeps_last_good_frame():
    calls: list = []
    gibs = {"GOES-West_ABI_Band13_Clean_Infrared": "2026-11-23T09:30:00Z"}
    wms = {"mtg_fd:ir105_hrfi": "2026-11-23T09:40:00Z", "msg_iodc:ir108": "2026-11-23T09:30:00Z"}
    t = [0.0]
    svc = IRFrames(httpx.AsyncClient(transport=_transport(gibs, wms, calls)), clock=lambda: t[0], now=lambda: NOW)
    first = {s["id"]: s for s in asyncio.run(svc.frames())["sources"]}
    assert first["mtg"]["time"] == "2026-11-23T09:40:00Z"
    assert first["goes_east"]["time"] is None and "no frame" in first["goes_east"]["error"]
    wms["mtg_fd:ir105_hrfi"] = None
    wms["msg_fes:ir108"] = None
    t[0] = 10_000
    second = {s["id"]: s for s in asyncio.run(svc.frames())["sources"]}
    assert second["mtg"]["time"] == "2026-11-23T09:40:00Z" and second["mtg"]["stale"] and second["mtg"]["error"]


def test_gibs_server_error_stops_probing():
    calls: list = []
    from osint.weather import ir
    src = ir.GeoSource("x", "X", "X", 0.0, "gibs", "BROKEN", 10, 6, "gibs", "")
    svc = IRFrames(httpx.AsyncClient(transport=_transport({}, {}, calls)), now=lambda: NOW)
    f = asyncio.run(svc._resolve(src))
    assert f.time is None and "500" in f.error and len(calls) == 1


def test_gibs_uses_newest_slot_when_previous_is_missing_and_needs_two_levels():
    calls: list = []
    from osint.weather import ir
    src = ir.GeoSource("x", "X", "X", -75.2, "gibs", "L", 10, 6, "gibs", "")
    partial = {"2026-11-23T09:40:00Z": {"0/0/0"}, "2026-11-23T09:30:00Z": {"0/0/0", "2"},
               "2026-11-23T09:20:00Z": set()}

    def handler(req):
        calls.append(str(req.url))
        ts, rest = str(req.url).split("/default/")[1].split("/", 1)
        zyx = rest.split("/", 1)[1].removesuffix(".png")
        have = partial.get(ts, set())
        ok = zyx in have or (zyx.startswith("2/") and "2" in have)
        return httpx.Response(200 if ok else 404)
    svc = IRFrames(httpx.AsyncClient(transport=httpx.MockTransport(handler)), now=lambda: NOW)
    f = asyncio.run(svc._resolve(src))
    # 09:40 has only its level-0 tile -> not ready; 09:30 complete, 09:20 missing -> 09:30
    assert iso(f.time) == "2026-11-23T09:30:00Z"
    assert any("/2/" in c for c in calls)


def test_tile_xy():
    from osint.weather.ir import tile_xy
    assert tile_xy(0.0, 0.0, 0) == (0, 0)
    assert tile_xy(0.0, -75.2, 2) == (1, 2)
    assert tile_xy(0.0, 140.7, 2) == (3, 2)
    assert tile_xy(85.0, 179.99, 3) == (7, 0)
