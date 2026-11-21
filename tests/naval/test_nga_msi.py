from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from osint.collectors.nga_msi import API_URL, NgaMsiCollector, parse_warning
from osint.config import ROOT, Settings
from osint.core.collector import CollectorContext
from osint.store.sqlite import SqliteStore

NOW = datetime(2026, 11, 21, 12, tzinfo=UTC)
TEXT = ("NORTH ARABIAN SEA. 1. AIRCRAFT CARRIER FLIGHT OPERATIONS 201500Z NOV TO 302359Z NOV "
        "IN AREA BOUND BY 18-00.00N 061-00.00E, 18-00.00N 063-00.00E, "
        "16-30.00N 063-00.00E, 16-30.00N 061-00.00E. 2. CANCEL THIS MSG 010000Z DEC 26.")
GOOD = {"msgYear": 2026, "msgNumber": 812, "navArea": "9", "subregion": "", "text": TEXT,
        "status": "A", "issueDate": "201200Z NOV 2026", "authority": "NAVCENT"}
OLD = {**GOOD, "msgNumber": 700, "issueDate": "011200Z OCT 2026",
       "text": "GUNNERY EXERCISES WITHIN 10 MILES OF 25-00N 055-00E."}
BAD = {"msgYear": 2026, "navArea": "4", "text": "no number"}
EMPTY = {**GOOD, "msgNumber": 9, "text": "   "}


def test_parse_warning_document():
    d = parse_warning(GOOD, NOW, ["CARRIER", "FLIGHT OPERATIONS"], ["GUNNERY"])
    assert d.kind == "nav_warning" and d.source == "nga_msi" and d.origin_id == "9-812-2026"
    assert d.published_at == datetime(2026, 11, 20, 12, tzinfo=UTC)
    assert d.attrs["keywords"] == ["CARRIER", "FLIGHT OPERATIONS"]
    assert d.attrs["geometries"][0]["type"] == "polygon"
    assert d.attrs["cancel_at"].startswith("2026-12-01")
    assert len(d.snippet) <= 300 and "812/26" in d.title
    old = parse_warning(OLD, NOW, [], ["GUNNERY"])
    assert old.published_at == NOW - timedelta(hours=47) and old.attrs["published_clamped"]
    assert old.attrs["issued_at"].startswith("2026-10-01")
    with pytest.raises(KeyError):
        parse_warning(BAD, NOW, [], [])
    with pytest.raises(ValueError):
        parse_warning(EMPTY, NOW, [], [])


@respx.mock
async def test_poll_partial_failure_cursor_and_no_reemit(tmp_path):
    route = respx.get(API_URL).mock(return_value=httpx.Response(
        200, json={"broadcast-warn": [GOOD, OLD, BAD, EMPTY, {**GOOD, "msgNumber": 5,
                                                             "status": "C"}]}))
    store = SqliteStore(tmp_path / "s.sqlite")
    try:
        async with httpx.AsyncClient() as http:
            ctx = CollectorContext(http=http, settings=Settings(_env_file=None, data_dir=tmp_path,
                                                                config_dir=ROOT / "config"),
                                   store=store)
            c = NgaMsiCollector({})
            b = await c.poll(ctx)
            assert sorted(d.origin_id for d in b.documents) == ["9-700-2026", "9-812-2026"]
            cur = store.get_cursor("nga_msi")
            assert set(cur["active"]) == {"9-812-2026", "9-700-2026", "9-9-2026"}
            assert route.called and route.calls[0].request.url.params["status"] == "A"
            again = await c.poll(ctx)
            assert again.documents == []          # already emitted, still active
            respx.get(API_URL).mock(return_value=httpx.Response(503, text="maintenance"))
            with pytest.raises(httpx.HTTPStatusError):
                await c.poll(ctx)
    finally:
        store.close()
