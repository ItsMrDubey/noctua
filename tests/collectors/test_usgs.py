from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import respx

from osint.collectors.usgs import FEED_URL, UsgsCollector, parse_feature
from osint.core.collector import CollectorContext
from osint.store.sqlite import SqliteStore

FEATURE = {
    "id": "us7000abcd",
    "properties": {"mag": 5.4, "place": "10 km S of Bandar Abbas, Iran", "time": 1763726400000,
                   "updated": 1763726500000, "felt": 3, "tsunami": 0, "type": "earthquake",
                   "magType": "mww", "url": "https://earthquake.usgs.gov/x"},
    "geometry": {"type": "Point", "coordinates": [56.27, 27.1, 10.0]},
}


def test_parse_feature():
    o = parse_feature(FEATURE)
    assert o.id == "quake:us7000abcd" and o.version == "1763726500000"
    assert o.ts == datetime(2025, 11, 21, 12, 0, tzinfo=UTC)
    assert (o.lat, o.lon, o.value) == (27.1, 56.27, 5.4)
    assert o.attrs["depth_km"] == 10.0 and o.attrs["event_type"] == "earthquake"
    assert parse_feature({"id": "x", "properties": {}, "geometry": None}) is None


@respx.mock
async def test_first_poll_backfills_day_then_hour(tmp_path):
    day = respx.get(FEED_URL.format(window="all_day")).mock(
        return_value=httpx.Response(200, json={"features": [FEATURE]}))
    hour = respx.get(FEED_URL.format(window="all_hour")).mock(
        return_value=httpx.Response(200, json={"features": []}))
    store = SqliteStore(tmp_path / "s.sqlite")
    async with httpx.AsyncClient() as http:
        ctx = CollectorContext(http=http, settings=SimpleNamespace(), store=store)
        c = UsgsCollector({})
        first = await c.poll(ctx)
        second = await c.poll(ctx)
    store.close()
    assert len(first.observations) == 1 and second.observations == []
    assert day.call_count == 1 and hour.call_count == 1
