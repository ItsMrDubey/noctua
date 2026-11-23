import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from osint.collectors.firms import FirmsCollector, confidence_label, parse_csv, parse_row

HEADER = ("latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,"
          "confidence,version,bright_ti5,frp,daynight\n")
ROW1 = "29.1,47.9,330.5,0.4,0.4,2026-11-21,0105,N20,VIIRS,h,2.0NRT,290.1,12.5,N\n"
ROW2 = "-3.2,120.5,310.0,0.4,0.4,2026-11-21,0510,N20,VIIRS,n,2.0NRT,285.0,,D\n"


@pytest.fixture(autouse=True)
def _midday(monkeypatch):
    """Polls ask for 2 days shortly after UTC midnight: pin the clock so URLs are stable."""
    monkeypatch.setattr("osint.collectors.firms.utcnow",
                        lambda: datetime(2026, 11, 21, 12, 0, tzinfo=UTC))


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


def test_days_overlap_after_utc_midnight():
    c = FirmsCollector({})
    assert c.days(datetime(2026, 11, 21, 2, 0, tzinfo=UTC)) == 2  # late detections of the 20th
    assert c.days(datetime(2026, 11, 21, 6, 0, tzinfo=UTC)) == 1
    assert FirmsCollector({"days": 3}).days(datetime(2026, 11, 21, 1, tzinfo=UTC)) == 3
    assert FirmsCollector({"midnight_overlap_h": 0}).days(datetime(2026, 11, 21, 0, tzinfo=UTC)) == 1


@respx.mock
async def test_unchanged_csv_is_not_resubmitted():
    base = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/k"
    route = respx.get(f"{base}/P1/world/1").mock(
        return_value=httpx.Response(200, text=HEADER + ROW1))
    c = FirmsCollector({"products": ["P1"]})
    async with httpx.AsyncClient() as http:
        assert len((await c.poll(_ctx(http))).observations) == 1
        assert (await c.poll(_ctx(http))).observations == []  # same CSV: skipped
        route.mock(return_value=httpx.Response(200, text=HEADER + ROW1 + ROW2))
        assert len((await c.poll(_ctx(http))).observations) == 2  # FIRMS updated


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


MODIS_HEADER = ("latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,instrument,"
                "confidence,version,bright_t31,frp,daynight\n")
MODIS_ROW = "10.5,20.5,340.2,1.0,1.0,2026-11-21,5,Terra,MODIS,85,6.1NRT,300.0,20.0,D\n"


def test_parse_modis_row():
    (o,) = parse_csv(MODIS_HEADER + MODIS_ROW, "MODIS_NRT")
    assert o.ts == datetime(2026, 11, 21, 0, 5, tzinfo=UTC)
    assert o.attrs["brightness_k"] == 340.2
    assert o.attrs["confidence"] == "High (85%)"
    assert o.value == 20.0 and o.source == "firms:MODIS_NRT"


def test_parse_csv_skips_bad_rows(caplog):
    bad = "1.0,2.0,300,0.4,0.4,not-a-date,0105,N20,VIIRS,h,2.0NRT,290,5,N\n"
    with caplog.at_level(logging.WARNING, logger="osint.collectors.firms"):
        obs = parse_csv(HEADER + bad + ROW1, "P1")
    assert len(obs) == 1 and obs[0].lat == 29.1
    assert len([r for r in caplog.records if "skipped 1" in r.getMessage()]) == 1


@respx.mock
async def test_http_error_does_not_leak_key(caplog):
    key = "SECRETKEY123"
    respx.get(url__startswith="https://firms.modaps.eosdis.nasa.gov/").mock(
        return_value=httpx.Response(403))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError) as exc:
            await FirmsCollector({"products": ["P1"]}).poll(_ctx(http, key=key))
    assert key not in str(exc.value) and "403" in str(exc.value)


@respx.mock
async def test_transport_error_does_not_leak_key():
    key = "SECRETKEY123"
    respx.get(url__startswith="https://firms.modaps.eosdis.nasa.gov/").mock(
        side_effect=httpx.ConnectError("boom connecting to /SECRETKEY123/P1"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError) as exc:
            await FirmsCollector({"products": ["P1"]}).poll(_ctx(http, key=key))
    assert key not in str(exc.value) and "ConnectError" in str(exc.value)


@respx.mock
async def test_partial_failure_logged_without_key(caplog):
    key = "SECRETKEY123"
    base = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}"
    respx.get(f"{base}/P1/world/1").mock(return_value=httpx.Response(200, text=HEADER + ROW1))
    respx.get(f"{base}/P2/world/1").mock(
        return_value=httpx.Response(200, text=f"Invalid MAP_KEY {key}."))
    async with httpx.AsyncClient() as http:
        with caplog.at_level(logging.WARNING, logger="osint.collectors.firms"):
            batch = await FirmsCollector({"products": ["P1", "P2"]}).poll(_ctx(http, key=key))
    assert len(batch.observations) == 1
    msgs = [r.getMessage() for r in caplog.records if r.name == "osint.collectors.firms"]
    assert any("P2" in m for m in msgs) and not any(key in m for m in msgs)


@respx.mock
async def test_key_redacted_before_truncation():
    key = "SECRETKEY123"
    body = "x" * 195 + key  # key straddles the 200-char cut
    respx.get(url__startswith="https://firms.modaps.eosdis.nasa.gov/").mock(
        return_value=httpx.Response(200, text=body))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError) as exc:
            await FirmsCollector({"products": ["P1"]}).poll(_ctx(http, key=key))
    assert "SECR" not in str(exc.value)
