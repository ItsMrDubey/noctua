import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from osint.collectors.eonet import API_URL, EonetCollector, parse_event

# trimmed from a live /api/v3/events?status=open&days=30 response (2026-11-23): one event
# per category, at most the first and the last two geometries
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "eonet_events.json").read_text(
    encoding="utf-8"))
NOW = datetime(2026, 11, 23, 4, 0, tzinfo=UTC)


def event(cat):
    return next(e for e in FIXTURE["events"] if e["categories"][0]["id"] == cat)


def test_parse_storm_uses_latest_fix():
    o = parse_event(event("severeStorms"), NOW)
    assert o.id == "hazard:eonet:EONET_26494" and o.source == "eonet"
    assert (o.lat, o.lon, o.value) == (17.2, 84.7, 30.0)
    a = o.attrs
    assert a["hazard"] == "tropical_cyclone" and a["name"] == "Tropical Cyclone Akvan"
    assert a["t_start"] == "2026-11-16T18:00:00+00:00"  # first fix
    assert a["t_end"] == a["updated_at"] == "2026-11-19T18:00:00+00:00"
    assert a["severity_text"] == "30 kts" and a["alert_level"] is None and a["current"]
    assert a["url"].startswith("https://www.metoc.navy.mil/")


def test_parse_wildfire_and_excluded_iceberg():
    o = parse_event(event("wildfires"), NOW)
    assert o.attrs["hazard"] == "wildfire" and o.value == 500.0
    assert o.attrs["agency"] == "IRWIN"
    assert parse_event(event("seaLakeIce"), NOW) is None
    assert parse_event(event("seaLakeIce"), NOW, exclude=()).attrs["hazard"] == "sea_lake_ice"


def test_parse_polygon_geometry():
    e = {"id": "EONET_1", "title": "Volcano X", "categories": [{"id": "volcanoes"}],
         "sources": [], "closed": "2026-11-22T00:00:00Z",
         "geometry": [{"date": "2026-11-21T00:00:00Z", "type": "Polygon",
                       "coordinates": [[[10, 0], [12, 0], [12, 2], [10, 2], [10, 0]]]}]}
    o = parse_event(e, NOW)
    assert o.lon == pytest.approx(10.8) and o.lat == pytest.approx(0.8)
    assert o.attrs["polygon"][0] == [10.0, 0.0] and not o.attrs["current"]
    assert o.attrs["t_end"] == "2026-11-22T00:00:00+00:00"
    assert parse_event({**e, "geometry": []}, NOW) is None


@respx.mock
async def test_poll_bounds_window_and_filters():
    route = respx.get(API_URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    async with httpx.AsyncClient() as http:
        batch = await EonetCollector({}).poll(SimpleNamespace(http=http))
    assert sorted(o.attrs["hazard"] for o in batch.observations) == [
        "tropical_cyclone", "wildfire"]
    q = route.calls[0].request.url.params
    assert q["status"] == "open" and q["days"] == "30"


@respx.mock
async def test_poll_rejects_unexpected_payload():
    respx.get(API_URL).mock(return_value=httpx.Response(200, json={"error": "x"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TypeError):
            await EonetCollector({}).poll(SimpleNamespace(http=http))


def test_prescribed_burns_excluded_by_title():
    import re

    from osint.collectors.eonet import DEFAULT_EXCLUDE_TITLE

    burn = {**event("wildfires"), "title": "Prescribed Fire STEELY MILL RX, El Dorado, California"}
    rx = re.compile(DEFAULT_EXCLUDE_TITLE)
    assert parse_event(burn, NOW, exclude_title=rx) is None
    assert parse_event(event("wildfires"), NOW, exclude_title=rx) is not None
