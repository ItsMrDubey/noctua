"""Parsers against real responses (captured 2026-11-23) and the cache / background-fetch flow."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from osint.profiles import lookups as L

FX = Path(__file__).parent / "fixtures"


def fx(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def test_parse_hexdb():
    d = L.parse_hexdb(fx("hexdb_43c38c.json"))
    assert d == {"hex": "43C38C", "reg": "ZZ664", "manufacturer": "Boeing", "icao_type": "R135",
                 "type": "RC-135W Rivet Joint", "owner": "Royal Air Force", "operator_flag": "RRR"}
    assert L.parse_hexdb({}) is None


def test_parse_adsbdb_aircraft_and_unknown():
    d = L.parse_adsbdb_aircraft(fx("adsbdb_43c38c.json"))
    assert d["reg"] == "ZZ664" and d["owner"] == "Royal Air Force" and d["owner_country"] == "United Kingdom"
    assert "url_photo" not in d  # airport-data photos have no stated licence: not used
    assert L.parse_adsbdb_aircraft(fx("adsbdb_unknown.json")) is None


def test_parse_adsbdb_route():
    r = L.parse_adsbdb_route(fx("adsbdb_route_baw123.json"))
    assert r["airline"]["name"] == "British Airways"
    assert r["origin"]["icao_code"] == "EGLL" and r["destination"]["icao_code"] == "OTHH"


def test_parse_planespotters_keeps_urls_unchanged():
    raw = fx("planespotters_43c38c.json")
    d = L.parse_planespotters(raw)
    assert d["image"] == raw["photos"][0]["thumbnail_large"]["src"]
    assert d["link"] == raw["photos"][0]["link"] and d["credit"] == "Alon Varshaver"
    assert L.parse_planespotters(fx("planespotters_empty.json")) is None


def test_parse_commons_requires_identifier_in_title():
    d = L.parse_commons(fx("commons_ra85041.json"), "RA-85041")
    assert "RA-85041" in d["title"] and d["licence"].startswith("CC") and d["credit"]
    assert d["image"].startswith("https://") and d["link"].startswith("https://")
    assert "<" not in d["credit"]  # HTML stripped from extmetadata
    assert L.parse_commons(fx("commons_ra85041.json"), "RA-99999") is None


@respx.mock
async def test_fetchers_hit_the_documented_urls():
    respx.get("https://hexdb.io/api/v1/aircraft/43c38c").mock(return_value=httpx.Response(200, json=fx("hexdb_43c38c.json")))
    respx.get("https://hexdb.io/api/v1/aircraft/ffffff").mock(return_value=httpx.Response(404, json={}))
    respx.get("https://api.planespotters.net/pub/photos/hex/abcdef").mock(return_value=httpx.Response(200, json={"photos": []}))
    respx.get("https://api.planespotters.net/pub/photos/reg/ZZ664").mock(return_value=httpx.Response(200, json=fx("planespotters_43c38c.json")))
    async with httpx.AsyncClient() as http:
        assert (await L.f_hexdb(http, "43c38c"))["reg"] == "ZZ664"
        assert await L.f_hexdb(http, "ffffff") is None
        ps = await L.f_planespotters(http, "abcdef|ZZ664")  # hex miss -> registration
        assert ps["matched_on"] == "reg"


async def test_peek_schedules_then_serves_from_cache(tmp_path):
    calls = []

    async def fake(http, arg):
        calls.append(arg)
        return {"reg": "ZZ664"} if arg == "43c38c" else None

    lk = L.Lookups(tmp_path / "c.sqlite", "test@example.org", client=httpx.AsyncClient(),
                   fetchers={"hexdb": fake})
    try:
        assert lk.peek("hexdb", "43c38c")["status"] == "pending"
        assert lk.peek("hexdb", "43c38c")["status"] == "pending"  # one fetch in flight
        await asyncio.gather(*lk._inflight.values())
        r = lk.peek("hexdb", "43c38c")
        assert r["status"] == "ok" and r["data"] == {"reg": "ZZ664"} and r["fetched_at"]
        assert (await lk.fetch_now("hexdb", "000000"))["status"] == "not_found"
        assert calls == ["43c38c", "000000"]
        assert lk.peek("hexdb", None)["status"] == "skipped"
        assert "test@example.org" in lk.ua
    finally:
        await lk.aclose()


async def test_rate_limit_backs_off_the_source(tmp_path):
    async def limited(http, arg):
        raise L.RateLimited(120)

    lk = L.Lookups(tmp_path / "c.sqlite", None, client=httpx.AsyncClient(), fetchers={"adsbdb": limited})
    try:
        assert (await lk.fetch_now("adsbdb", "a"))["status"] == "rate_limited"
        assert lk.peek("adsbdb", "b")["status"] == "rate_limited"  # blocked for all keys
    finally:
        await lk.aclose()


async def test_errors_are_cached_briefly(tmp_path):
    async def boom(http, arg):
        raise httpx.ConnectError("down")

    lk = L.Lookups(tmp_path / "c.sqlite", None, client=httpx.AsyncClient(), fetchers={"commons": boom})
    try:
        r = await lk.fetch_now("commons", "IMO 1234567")
        assert r["status"] == "error" and "ConnectError" in r["error"]
    finally:
        await lk.aclose()


@pytest.mark.parametrize("sid", list(L.SPECS))
def test_specs_are_attributed(sid):
    s = L.SPECS[sid]
    assert s.licence and s.url.startswith("https://") and s.attribution and s.commercial in ("ok", "review", "no")
    assert s.id != "planespotters" or s.ttl_ok_s < 24 * 3600  # terms: JSON cached <= 24 h
