import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import respx

from osint.collectors.gdacs import API_URL, GdacsCollector, parse_feature

# trimmed from a live geteventlist/SEARCH response (2026-11-23): one feature per
# (eventtype, alertlevel) seen that week
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "gdacs_search.json").read_text(
    encoding="utf-8"))
NOW = datetime(2026, 11, 23, 4, 0, tzinfo=UTC)


def feature(code, level="Green"):
    return next(f for f in FIXTURE["features"] if f["properties"]["eventtype"] == code
                and f["properties"]["alertlevel"] == level)


def test_parse_earthquake():
    o = parse_feature(feature("EQ"), NOW)
    assert o.id == "hazard:gdacs:EQ:1578806" and o.type == "hazard" and o.source == "gdacs"
    assert (o.lat, o.lon, o.value) == (-13.5634, 172.2236, 4.9)
    a = o.attrs
    assert a["hazard"] == "earthquake" and a["alert_level"] == "green"
    assert a["t_start"] == "2026-11-23T03:07:52+00:00" and a["precision"] == "point"
    assert a["severity_text"].startswith("Magnitude 4.9M")
    assert a["url"].startswith("https://www.gdacs.org/report.aspx?eventid=1578806")
    assert o.ts == datetime(2026, 11, 23, 3, 27, 45, tzinfo=UTC)  # datemodified
    assert o.version == "1748225:green:2026-11-23T03:27:45"


def test_parse_types_and_long_running_ts_clamped():
    tc = parse_feature(feature("TC"), NOW)
    assert tc.attrs["hazard"] == "tropical_cyclone" and tc.attrs["name"] == (
        "Tropical Cyclone XAVIER-26") and tc.attrs["iso3"] == "COK"
    fl = parse_feature(feature("FL"), NOW)
    assert fl.attrs["hazard"] == "flood" and fl.attrs["iso2"] == ["CL"]
    assert fl.attrs["iso3s"] == ["CHL"]
    assert parse_feature(feature("WF"), NOW).attrs["hazard"] == "wildfire"
    dr = parse_feature(feature("DR", "Orange"), NOW)
    assert dr.attrs["alert_level"] == "orange" and dr.attrs["glide"] == "DR-2025-000172-SOM"
    assert dr.attrs["t_start"].startswith("2022-09-21")  # real start kept in attrs ...
    late = parse_feature(feature("DR", "Orange"), NOW + timedelta(days=10))
    assert late.ts == NOW + timedelta(days=10) - timedelta(hours=47)  # ... ts clamped


def test_parse_rejects_featureless():
    assert parse_feature({"properties": {"eventtype": "EQ"}, "geometry": None}, NOW) is None


@respx.mock
async def test_poll_pages_until_short_page_and_dedupes(caplog):
    page1 = {"features": [feature("EQ")] * 50 + [feature("TC")] * 50}
    page2 = {"features": [feature("FL"), {"properties": {"eventtype": "EQ", "eventid": "x"},
                                          "geometry": {"coordinates": [1, 2]}}]}
    route = respx.get(API_URL).mock(side_effect=[httpx.Response(200, json=page1),
                                                 httpx.Response(200, json=page2)])
    async with httpx.AsyncClient() as http:
        with caplog.at_level(logging.WARNING, logger="osint.collectors.gdacs"):
            batch = await GdacsCollector({}).poll(SimpleNamespace(http=http))
    assert [o.attrs["code"] for o in batch.observations] == ["EQ", "TC", "FL"]
    assert route.call_count == 2
    q = route.calls[0].request.url.params
    assert q["eventlist"] == "EQ;TC;FL;VO;DR;WF" and q["pagenumber"] == "1"
    assert q["alertlevel"] == "Green;Orange;Red"
    assert any("skipped 1" in r.getMessage() for r in caplog.records)


@respx.mock
async def test_poll_stops_on_204_no_content():
    respx.get(API_URL).mock(return_value=httpx.Response(204))
    async with httpx.AsyncClient() as http:
        batch = await GdacsCollector({}).poll(SimpleNamespace(http=http))
    assert batch.observations == []
