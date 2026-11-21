from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from osint.collectors.firms import FirmsCollector, confidence_label, parse_row

HEADER = ("latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,"
          "confidence,version,bright_ti5,frp,daynight\n")
ROW1 = "29.1,47.9,330.5,0.4,0.4,2026-11-21,0105,N20,VIIRS,h,2.0NRT,290.1,12.5,N\n"
ROW2 = "-3.2,120.5,310.0,0.4,0.4,2026-11-21,0510,N20,VIIRS,n,2.0NRT,285.0,,D\n"


def _ctx(http, key="k"):
    return SimpleNamespace(http=http, settings=SimpleNamespace(firms_map_key=key))


def test_confidence_label():
    assert confidence_label("h") == "High"
    assert confidence_label("85") == "High (85%)"
    assert confidence_label("10") == "Low (10%)"
    assert confidence_label("") == ""


def test_parse_row():
    import csv
    import io
    row = next(csv.DictReader(io.StringIO(HEADER + ROW1)))
    o = parse_row(row, "VIIRS_NOAA20_NRT")
    assert o.type == "fire" and o.ts == datetime(2026, 11, 21, 1, 5, tzinfo=UTC)
    assert (o.lat, o.lon, o.value) == (29.1, 47.9, 12.5)
    assert o.attrs["confidence"] == "High" and o.attrs["brightness_k"] == 330.5
    assert o.id.startswith("fire:") and o.id == parse_row(row, "VIIRS_NOAA20_NRT").id
    assert o.id != parse_row(row, "VIIRS_SNPP_NRT").id


@respx.mock
async def test_poll_tolerates_one_failing_product():
    base = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/k"
    respx.get(f"{base}/P1/world/1").mock(return_value=httpx.Response(200, text=HEADER + ROW1 + ROW2))
    respx.get(f"{base}/P2/world/1").mock(return_value=httpx.Response(200, text="Invalid MAP_KEY."))
    c = FirmsCollector({"products": ["P1", "P2"]})
    async with httpx.AsyncClient() as http:
        batch = await c.poll(_ctx(http))
    assert len(batch.observations) == 2
    assert batch.observations[1].value is None  # empty frp


@respx.mock
async def test_poll_raises_when_all_fail():
    respx.get(url__startswith="https://firms.modaps.eosdis.nasa.gov/").mock(
        return_value=httpx.Response(200, text="Invalid MAP_KEY."))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="all FIRMS products failed"):
            await FirmsCollector({"products": ["P1"]}).poll(_ctx(http))


async def test_poll_requires_key():
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="FIRMS_MAP_KEY"):
            await FirmsCollector({}).poll(_ctx(http, key=""))
