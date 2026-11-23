from types import SimpleNamespace

import httpx
import pytest
import respx

from osint.collectors.adsb_naval import AdsbNavalCollector
from osint.config import ROOT
from osint.enrich.military import MilDb
from osint.naval.capture import NavalCapture

LOL = "https://lol.test/v2"
FI = "https://fi.test/api/v2"
NOW_MS = 1795900000000


def ac(hx, t, flight="", mil=False, **kw):
    return {"hex": hx, "t": t, "flight": flight, "dbFlags": 1 if mil else 0, "lat": 36.0,
            "lon": -74.0, "alt_baro": 900, "gs": 140, "seen_pos": 1.0, **kw}


class Ctx(SimpleNamespace):
    def get_cursor(self, name):
        return self.cur.get(name)

    def set_cursor(self, name, v):
        self.cur[name] = v


async def nosleep(_s):
    return None


def make(cfg=None, clock=None):
    c = AdsbNavalCollector({"lol_base": LOL, "fi_base": FI, "types": {"V22": 1, "E2": 1},
                            "regions": [{"name": "VACAPES", "lat": 36, "lon": -74, "dist": 250}],
                            "_capture": NavalCapture.from_config_dir(ROOT / "config"),
                            "_mil_db": MilDb({}), **(cfg or {})})
    c.sleep = nosleep
    t = {"now": 1000.0}
    c._clock = clock or (lambda: t["now"])
    c._wall = lambda: NOW_MS / 1000 + t["now"]
    return c, t


@pytest.fixture
def ctx():
    return Ctx(cur={}, settings=SimpleNamespace())


@respx.mock
async def test_type_region_follow_capture_only_unflagged_naval(ctx):
    respx.get(f"{LOL}/type/V22").mock(return_value=httpx.Response(200, headers={
        "x-ratelimit-remaining": "300", "x-ratelimit-reset": "40000"}, json={"now": NOW_MS, "ac": [
            ac("ae0001", "V22", "TITAN11"), ac("ae0002", "V22", "MARINE1", mil=True)]}))
    respx.get(f"{FI}/lat/36/lon/-74/dist/250").mock(return_value=httpx.Response(200, json={
        "now": NOW_MS / 1000, "aircraft": [
            ac("a00001", "B738", "AAL12"),               # airliner: not wanted
            ac("a00002", "BE20", "RAWHIDE34"),           # listed callsign, unlisted type
            ac("ae0003", "H60", "", mil=True)]}))        # flagged: adsb already has it
    async with httpx.AsyncClient() as http:
        ctx.http = http
        c, t = make()
        b = await c.poll(ctx)
    got = {p.track_id: p for p in b.positions}
    assert set(got) == {"air:ae0001", "air:a00002"}
    assert got["air:ae0001"].source == "adsb_naval:adsb.lol"
    why = {tr.track_id: tr.extra["naval_capture"] for tr in b.tracks}
    assert why["air:ae0001"] == "type V22" and why["air:a00002"].startswith("callsign RAWHIDE34")
    st = ctx.cur["adsb_naval"]
    assert st["lol_used"] == 1 and set(st["follow"]) == {"ae0001", "a00002"}
    assert c.lol_remaining == 300

    # next poll a minute later: no type query yet (budget spacing), no regional sweep
    # (interval), but the captured hexes are followed with ONE multi-hex adsb.fi request
    follow = respx.get(f"{FI}/hex/ae0001,a00002").mock(return_value=httpx.Response(200, json={
        "now": NOW_MS / 1000 + 60, "ac": [ac("ae0001", "V22", "TITAN11", lat=36.1)]}))
    async with httpx.AsyncClient() as http:
        ctx.http = http
        t["now"] += 60
        b2 = await c.poll(ctx)
    assert follow.call_count == 1 and [p.lat for p in b2.positions] == [36.1]
    assert respx.calls.call_count == 3


@respx.mock
async def test_type_budget_and_shared_reserve_are_respected(ctx):
    route = respx.get(f"{LOL}/type/V22").mock(return_value=httpx.Response(200, headers={
        "x-ratelimit-remaining": "120", "x-ratelimit-reset": "40000"}, json={"ac": []}))
    respx.get(url__startswith=FI).mock(return_value=httpx.Response(200, json={"ac": []}))
    async with httpx.AsyncClient() as http:
        ctx.http = http
        c, t = make({"type_budget_per_day": 1000, "lol_reserve": 150})
        await c.poll(ctx)
        t["now"] += 100000
        await c.poll(ctx)      # shared remaining (120) is below the reserve: no type query
    assert route.call_count == 1
    c2, t2 = make({"type_budget_per_day": 2})
    c2.lol_used = 2
    c2.day = "2026-11-23"
    assert not c2._type_due(t2["now"])


@respx.mock
async def test_rate_limit_stops_the_adsb_fi_cycle_and_all_failures_raise(ctx):
    respx.get(f"{LOL}/type/V22").mock(return_value=httpx.Response(429, headers={
        "retry-after": "600"}))
    respx.get(f"{FI}/lat/36/lon/-74/dist/250").mock(return_value=httpx.Response(429))
    async with httpx.AsyncClient() as http:
        ctx.http = http
        c, t = make()
        with pytest.raises(RuntimeError, match="every request failed"):
            await c.poll(ctx)
    assert c.lol_next >= t["now"] + 600


def test_capture_filter():
    cap = NavalCapture.from_config_dir(ROOT / "config")
    assert cap.reason(ac("x", "P8")) == "type P8"
    assert cap.reason(ac("x", "P8", mil=True)) is None
    assert cap.reason(ac("x", "A320", "RAWHIDE 7")) is not None
    assert cap.reason(ac("x", "A320", "RYR123")) is None
