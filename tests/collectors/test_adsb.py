import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from osint.collectors.adsb import (
    AdsbCollector,
    opensky_to_readsb,
    pacing_delay,
    parse_aircraft,
)
from osint.enrich.military import MilDb

NOW_MS = 1763726400000  # 2025-11-21T12:00:00Z


def test_mildb_from_csv(tmp_path):
    p = tmp_path / "db.csv"
    p.write_text(
        "$ICAO,$Registration,$Operator,$Type,$ICAO Type,#CMPG,$Tag 1,$#Tag 2,$#Tag 3,Category,$#Link\n"
        "AE1234,05-5140,USAF,Boeing C-17A,C17,Mil,Cargo,x,y,Big Hello,https://x\n",
        encoding="utf-8",
    )
    db = MilDb.from_csv(p)
    assert len(db) == 1
    assert db.get("ae1234")["operator"] == "USAF"
    assert MilDb.from_csv(tmp_path / "missing.csv").get("ae1234") is None


def test_parse_aircraft_enriches_from_db():
    db = MilDb({"ae1234": {"reg": "05-5140", "operator": "USAF", "type": "Boeing C-17A",
                           "icao_type": "C17", "category": "Big Hello", "tag1": "Cargo"}})
    pos, track = parse_aircraft(
        {"hex": "AE1234", "flight": "RCH123 ", "lat": 10.0, "lon": 20.0, "alt_baro": 30000,
         "gs": 450.0, "track": 90.0, "seen_pos": 2.0, "squawk": "1234"},
        NOW_MS / 1000, db, "adsb:test")
    assert pos.track_id == "air:ae1234"
    assert pos.ts == datetime.fromtimestamp(NOW_MS / 1000 - 2.0, UTC)
    assert (pos.alt_ft, pos.speed_kts, pos.heading_deg) == (30000, 450.0, 90.0)
    assert track.name == "RCH123" and track.reg == "05-5140" and track.type_code == "C17"
    assert track.operator == "USAF" and track.is_mil and track.high_priority
    assert track.extra == {"desc": "Boeing C-17A", "role": "Cargo", "squawk": "1234"}


def test_parse_aircraft_edge_cases():
    db = MilDb({})
    pos, track = parse_aircraft({"hex": "abc123", "alt_baro": "ground", "lat": 1, "lon": 1,
                                 "dbFlags": 1}, NOW_MS / 1000, db, "s")
    assert pos.alt_ft == 0 and track.is_mil and not track.high_priority
    pos, track = parse_aircraft({"hex": "abc124", "t": "F16"}, NOW_MS / 1000, db, "s")
    assert pos is None and track.is_mil
    assert parse_aircraft({"hex": "~2b0001", "lat": 1, "lon": 1}, NOW_MS / 1000, db, "s") is None


@respx.mock
async def test_poll_merges_endpoints_prefers_freshest():
    respx.get("https://a.test/v2/mil").mock(return_value=httpx.Response(200, json={
        "now": NOW_MS, "ac": [{"hex": "ae1234", "lat": 10.0, "lon": 20.0, "t": "C17",
                               "seen_pos": 5.0}]}))
    respx.get("https://b.test/v2/mil").mock(return_value=httpx.Response(200, json={
        "now": NOW_MS, "ac": [{"hex": "ae1234", "lat": 10.1, "lon": 20.1, "t": "C17",
                               "seen_pos": 1.0}]}))
    respx.get("https://c.test/v2/mil").mock(return_value=httpx.Response(503))
    c = AdsbCollector({"endpoints": {"a": "https://a.test/v2", "b": "https://b.test/v2",
                                     "c": "https://c.test/v2"}})
    c._mil_db = MilDb({})
    async with httpx.AsyncClient() as http:
        batch = await c.poll(SimpleNamespace(http=http))
    assert len(batch.positions) == 1 and len(batch.tracks) == 1
    assert batch.positions[0].lat == 10.1
    assert batch.positions[0].source == "adsb:b"


@respx.mock
async def test_poll_raises_when_all_endpoints_fail():
    respx.get("https://a.test/v2/mil").mock(return_value=httpx.Response(500))
    c = AdsbCollector({"endpoints": {"a": "https://a.test/v2"}})
    c._mil_db = MilDb({})
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="all ADS-B endpoints failed"):
            await c.poll(SimpleNamespace(http=http))


@respx.mock
async def test_partial_endpoint_failure_is_logged(caplog):
    respx.get("https://a.test/v2/mil").mock(return_value=httpx.Response(200, json={
        "now": NOW_MS, "ac": [{"hex": "ae1234", "lat": 1.0, "lon": 2.0}]}))
    respx.get("https://c.test/v2/mil").mock(return_value=httpx.Response(503))
    c = AdsbCollector({"endpoints": {"a": "https://a.test/v2", "c": "https://c.test/v2"}})
    c._mil_db = MilDb({})
    async with httpx.AsyncClient() as http:
        with caplog.at_level(logging.WARNING, logger="osint.collectors.adsb"):
            await c.poll(SimpleNamespace(http=http))
    msgs = [r.getMessage() for r in caplog.records if r.name == "osint.collectors.adsb"]
    assert len(msgs) == 1 and "c:" in msgs[0] and "503" in msgs[0]


@respx.mock
async def test_bad_aircraft_skipped_and_counted(caplog):
    respx.get("https://a.test/v2/mil").mock(return_value=httpx.Response(200, json={
        "now": NOW_MS, "ac": [{"hex": "bad001", "lat": "oops", "lon": 2.0},
                              {"hex": "bad002", "lat": 1.0, "lon": 2.0, "seen_pos": "x"},
                              {"hex": "ae1234", "lat": 1.0, "lon": 2.0}]}))
    c = AdsbCollector({"endpoints": {"a": "https://a.test/v2"}})
    c._mil_db = MilDb({})
    async with httpx.AsyncClient() as http:
        with caplog.at_level(logging.WARNING, logger="osint.collectors.adsb"):
            batch = await c.poll(SimpleNamespace(http=http))
    assert [p.track_id for p in batch.positions] == ["air:ae1234"]
    assert len([r for r in caplog.records if "skipped 2" in r.getMessage()]) == 1


@respx.mock
async def test_mildb_loaded_in_thread(tmp_path, monkeypatch):
    import threading
    seen = []
    orig = MilDb.from_csv

    def spy(path):
        seen.append(threading.current_thread())
        return orig(path)

    monkeypatch.setattr(MilDb, "from_csv", staticmethod(spy))
    respx.get("https://a.test/v2/mil").mock(
        return_value=httpx.Response(200, json={"now": NOW_MS, "ac": []}))
    c = AdsbCollector({"endpoints": {"a": "https://a.test/v2"}})
    ctx_settings = SimpleNamespace(reference_dir=tmp_path)
    async with httpx.AsyncClient() as http:
        await c.poll(SimpleNamespace(http=http, settings=ctx_settings))
    assert seen and seen[0] is not threading.main_thread()


# ------------------------------------------------------- failover, backoff, rate limits


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _collector(endpoints, **cfg):
    c = AdsbCollector({"endpoints": endpoints, **cfg})
    c._mil_db = MilDb({"ae0001": {"icao_type": "C17"}})
    c._clock = Clock()
    return c


AC = {"now": NOW_MS, "ac": [{"hex": "ae1234", "lat": 1.0, "lon": 2.0, "seen_pos": 1.0}]}
T = NOW_MS // 1000
# trimmed from a live OpenSky /api/states/all response (2026-11-23); first hex swapped for
# one in the test military DB
OPENSKY = {"time": T, "states": [
    ["ae0001", "RCH123  ", "United States", T - 4, T - 1, 9.0295, 45.8385, 11574.78, False,
     247.3, 3.1, 0, None, 11483.34, "1000", False, 0],
    ["3c4b32", "DLH573  ", "Germany", T, T, 9.0, 45.0, 11000.0, False, 240.0, 3.0, 0, None,
     11000.0, "1000", False, 0]]}


def test_opensky_state_converted_to_readsb_shape():
    ac = opensky_to_readsb(OPENSKY["states"][0], float(T))
    assert ac["hex"] == "ae0001" and ac["lat"] == 45.8385 and ac["lon"] == 9.0295
    assert ac["alt_baro"] == 37975 and ac["gs"] == 480.7 and ac["seen_pos"] == 4
    ground = ["x", None, None, None, None, None, None, None, True, None, None]
    assert opensky_to_readsb(ground, 0)["alt_baro"] == "ground"
    assert opensky_to_readsb([], 0) is None


def test_pacing_delay_spreads_budget_until_reset():
    h = httpx.Headers({"x-ratelimit-remaining": "460", "x-ratelimit-reset": "72577"})
    assert pacing_delay(h) == pytest.approx(157.78, rel=1e-3)
    h = httpx.Headers({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "60"})
    assert pacing_delay(h) == 60
    assert pacing_delay(httpx.Headers({})) == 0


@respx.mock
async def test_forbidden_endpoint_backs_off_and_warns_once(caplog):
    ok = respx.get("https://a.test/v2/mil").mock(return_value=httpx.Response(200, json=AC))
    denied = respx.get("https://b.test/v2/mil").mock(return_value=httpx.Response(
        403, json={"error": "Please contact us"}))
    c = _collector({"a": "https://a.test/v2", "b": "https://b.test/v2"})
    async with httpx.AsyncClient() as http:
        with caplog.at_level(logging.WARNING, logger="osint.collectors.adsb"):
            for _ in range(5):
                batch = await c.poll(SimpleNamespace(http=http))
                c._clock.t += 30
    assert len(batch.positions) == 1
    assert ok.call_count == 5 and denied.call_count == 1  # 6 h backoff after the 403
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1 and "b:" in warns[0] and "403" in warns[0]
    assert c.status()["b"]["backoff_s"] > 3600


@respx.mock
async def test_transient_failure_backoff_then_recovery_logged(caplog):
    respx.get("https://a.test/v2/mil").mock(return_value=httpx.Response(200, json=AC))
    flaky = respx.get("https://b.test/v2/mil").mock(side_effect=[
        httpx.Response(503), httpx.Response(200, json=AC)])
    c = _collector({"a": "https://a.test/v2", "b": "https://b.test/v2"})
    async with httpx.AsyncClient() as http:
        with caplog.at_level(logging.INFO, logger="osint.collectors.adsb"):
            await c.poll(SimpleNamespace(http=http))
            c._clock.t += 10  # still inside the 30 s backoff
            await c.poll(SimpleNamespace(http=http))
            assert flaky.call_count == 1
            c._clock.t += 30
            await c.poll(SimpleNamespace(http=http))
    assert flaky.call_count == 2
    assert any("recovered" in r.getMessage() for r in caplog.records)


@respx.mock
async def test_rate_limit_headers_pace_the_endpoint():
    paced = respx.get("https://a.test/v2/mil").mock(return_value=httpx.Response(
        200, json=AC, headers={"x-ratelimit-remaining": "10", "x-ratelimit-reset": "1000"}))
    respx.get("https://b.test/v2/mil").mock(return_value=httpx.Response(200, json=AC))
    c = _collector({"a": "https://a.test/v2", "b": "https://b.test/v2"})
    async with httpx.AsyncClient() as http:
        for _ in range(5):  # 30 s apart; a may only be called every 100 s
            await c.poll(SimpleNamespace(http=http))
            c._clock.t += 30
    assert paced.call_count == 2


@respx.mock
async def test_429_honours_retry_after():
    limited = respx.get("https://a.test/v2/mil").mock(return_value=httpx.Response(
        429, headers={"Retry-After": "120"}))
    respx.get("https://b.test/v2/mil").mock(return_value=httpx.Response(200, json=AC))
    c = _collector({"a": "https://a.test/v2", "b": "https://b.test/v2"})
    async with httpx.AsyncClient() as http:
        await c.poll(SimpleNamespace(http=http))
        c._clock.t += 100
        await c.poll(SimpleNamespace(http=http))
        c._clock.t += 30
        await c.poll(SimpleNamespace(http=http))
    assert limited.call_count == 2


@respx.mock
async def test_opensky_fallback_only_when_primaries_fail_and_rate_limited():
    primary = respx.get("https://a.test/v2/mil").mock(return_value=httpx.Response(500))
    sky = respx.get("https://sky.test/api/states/all").mock(
        return_value=httpx.Response(200, json=OPENSKY))
    c = _collector({"a": "https://a.test/v2",
                    "sky": {"url": "https://sky.test/api", "kind": "opensky",
                            "fallback": True, "min_interval_s": 900}})
    async with httpx.AsyncClient() as http:
        batch = await c.poll(SimpleNamespace(http=http))
        # only the military-DB hex survives the filter
        assert [p.track_id for p in batch.positions] == ["air:ae0001"]
        assert batch.positions[0].source == "adsb:sky" and batch.tracks[0].is_mil
        c._clock.t += 20  # primary still backing off, fallback inside its 15 min interval
        with pytest.raises(RuntimeError, match="all ADS-B endpoints failed"):
            await c.poll(SimpleNamespace(http=http))
    assert primary.call_count == 1 and sky.call_count == 1


@respx.mock
async def test_fallback_not_polled_while_a_primary_works():
    respx.get("https://a.test/v2/mil").mock(return_value=httpx.Response(200, json=AC))
    sky = respx.get("https://sky.test/api/states/all").mock(
        return_value=httpx.Response(200, json=OPENSKY))
    c = _collector({"a": "https://a.test/v2",
                    "sky": {"url": "https://sky.test/api", "kind": "opensky",
                            "fallback": True}})
    async with httpx.AsyncClient() as http:
        await c.poll(SimpleNamespace(http=http))
    assert sky.call_count == 0


@respx.mock
async def test_requests_carry_project_user_agent():
    route = respx.get("https://a.test/v2/mil").mock(return_value=httpx.Response(200, json=AC))
    c = _collector({"a": "https://a.test/v2"})
    async with httpx.AsyncClient() as http:
        await c.poll(SimpleNamespace(http=http))
    assert route.calls[0].request.headers["User-Agent"].startswith("osint-poc/")


def test_disabled_endpoint_dropped_and_defaults_exclude_airplanes_live():
    c = AdsbCollector({"endpoints": {"a": "https://a.test/v2",
                                     "b": {"url": "https://b.test/v2", "enabled": False}}})
    assert [e.name for e in c.endpoints] == ["a"]
    assert "airplanes.live" not in {e.name for e in AdsbCollector({}).endpoints}
