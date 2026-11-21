import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from osint.collectors.adsb import AdsbCollector, parse_aircraft
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
