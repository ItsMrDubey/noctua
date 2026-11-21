import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
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


class DictCtx:
    def __init__(self, http, cursor=None):
        self.http, self.cursor = http, cursor

    def get_cursor(self, name):
        return self.cursor

    def set_cursor(self, name, cursor):
        self.cursor = cursor


def _mock_all_windows():
    return {w: respx.get(FEED_URL.format(window=w)).mock(
        return_value=httpx.Response(200, json={"features": []}))
        for w in ("all_hour", "all_day", "all_week", "all_month")}


@pytest.mark.parametrize(("age", "window"), [
    (timedelta(minutes=2), "all_hour"),
    (timedelta(hours=2), "all_day"),
    (timedelta(days=3), "all_week"),
    (timedelta(days=10), "all_month"),
])
@respx.mock
async def test_window_follows_cursor_age(age, window):
    routes = _mock_all_windows()
    cursor = {"last_poll": (datetime.now(UTC) - age).isoformat()}
    async with httpx.AsyncClient() as http:
        await UsgsCollector({}).poll(DictCtx(http, cursor))
    assert [w for w, r in routes.items() if r.called] == [window]


@respx.mock
async def test_last_poll_recorded_only_after_success():
    old = {"last_poll": (datetime.now(UTC) - timedelta(hours=2)).isoformat()}
    respx.get(FEED_URL.format(window="all_day")).mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http:
        ctx = DictCtx(http, dict(old))
        with pytest.raises(httpx.HTTPStatusError):
            await UsgsCollector({}).poll(ctx)
    assert ctx.cursor == old


@respx.mock
async def test_last_poll_is_poll_start_time():
    _mock_all_windows()
    before = datetime.now(UTC)
    async with httpx.AsyncClient() as http:
        ctx = DictCtx(http)
        await UsgsCollector({}).poll(ctx)
    assert datetime.fromisoformat(ctx.cursor["last_poll"]) >= before


@respx.mock
async def test_bad_feature_skipped_and_counted(caplog):
    bad = {"id": "bad", "properties": {"time": "nope"}, "geometry": {"coordinates": [1, 2]}}
    respx.get(FEED_URL.format(window="all_day")).mock(
        return_value=httpx.Response(200, json={"features": [bad, FEATURE]}))
    async with httpx.AsyncClient() as http:
        with caplog.at_level(logging.WARNING, logger="osint.collectors.usgs"):
            batch = await UsgsCollector({}).poll(DictCtx(http))
    assert [o.id for o in batch.observations] == ["quake:us7000abcd"]
    assert len([r for r in caplog.records if "skipped 1" in r.getMessage()]) == 1
