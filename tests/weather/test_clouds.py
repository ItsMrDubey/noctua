import asyncio

import httpx

from osint.weather import MAX_POINTS, CloudService, grid


def test_grid_scales_with_view_and_caps_points():
    step, nodes = grid((-74.5, 40.0, -73.5, 41.0))
    assert step == 0.25 and 0 < len(nodes) <= MAX_POINTS
    step2, nodes2 = grid((-80, 35, -65, 45))
    assert step2 == 2.0 and len(nodes2) <= MAX_POINTS
    assert all(-180 <= lo <= 180 for _, lo in nodes2)


def _client(calls):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        lats = req.url.params["latitude"].split(",")
        rows = [{"current": {"cloud_cover_low": 40, "cloud_cover_mid": 10, "cloud_cover_high": 90,
                             "wind_speed_10m": 12, "wind_direction_10m": 270}} for _ in lats]
        return httpx.Response(200, json=rows if len(rows) > 1 else rows[0])
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_fetches_missing_nodes_once_then_serves_cache():
    calls = []
    t = [0.0]
    svc = CloudService(_client(calls), clock=lambda: t[0])
    bbox = (-74.5, 40.0, -73.5, 41.0)
    a = asyncio.run(svc.clouds(bbox))
    assert a["count"] > 0 and a["items"][0]["high"] == 90 and not a["stale"]
    asyncio.run(svc.clouds(bbox))
    assert len(calls) == 1  # cached
    t[0] = 10_000
    asyncio.run(svc.clouds(bbox))
    assert len(calls) == 2  # expired -> refetched


def test_upstream_failure_is_stale_not_error():
    def boom(req):
        return httpx.Response(503)
    svc = CloudService(httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    out = asyncio.run(svc.clouds((-74.5, 40.0, -73.5, 41.0)))
    assert out["stale"] and out["count"] == 0 and "503" in out["error"]
