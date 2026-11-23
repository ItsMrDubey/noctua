import csv
import io
from types import SimpleNamespace

import httpx
import pytest
import respx

from osint.backfill import ucdp
from osint.collectors.ucdp_candidate import UcdpCandidateCollector
from osint.core.collector import CollectorContext
from osint.store.sqlite import SqliteStore
from tests.backfill.test_ucdp_adsb_backfill import GED_FIELDS, ged_row

BASE = "https://ucdp.uu.se/downloads/candidateged"
PAGE = (f'<a href="{BASE}/GEDEvent_v26_0_10.csv">csv</a>'
        f'<a href="{BASE}/GEDevent_v26_0_10.xlsx">xlsx</a>'
        f'<a href="{BASE}/GEDEvent_v26_01_26_09.csv">csv</a>'
        f'<a href="{BASE}/GEDEvent_v26_01_26_09.csv">dup</a>')


def _csv(*rows: dict) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=GED_FIELDS)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _ctx(http, tmp_path):
    return CollectorContext(http=http, settings=SimpleNamespace(),
                            store=SqliteStore(tmp_path / "s.sqlite"))


def test_candidate_urls():
    assert ucdp.candidate_urls(PAGE) == [f"{BASE}/GEDEvent_v26_0_10.csv",
                                         f"{BASE}/GEDEvent_v26_01_26_09.csv"]


@respx.mock
async def test_poll_imports_and_uses_conditional_get(tmp_path):
    respx.get(ucdp.DOWNLOADS_URL).mock(return_value=httpx.Response(200, text=PAGE))
    newest = respx.get(f"{BASE}/GEDEvent_v26_0_10.csv").mock(side_effect=[
        httpx.Response(200, content=_csv(ged_row(), ged_row(id="2", latitude="")),
                       headers={"ETag": '"e1"'}),
        httpx.Response(304),
    ])
    ytd = respx.get(f"{BASE}/GEDEvent_v26_01_26_09.csv").mock(side_effect=[
        httpx.Response(200, content=_csv(ged_row(id="1", best="5"), ged_row(id="644785",
                                                                             best="9")),
                       headers={"Last-Modified": "Sat, 21 Nov 2026 00:32:40 GMT"}),
        httpx.Response(304),
    ])
    c = UcdpCandidateCollector({})
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http, tmp_path)
        batch = await c.poll(ctx)
        assert batch.size() == 0  # events go straight to the store
        ev = ctx.store.get_event("ucdp-644785")
        assert ev["status"] == "historical" and ev["category"] == "armed_conflict"
        # the newest release is applied last and wins the upsert
        assert "1 killed" in ev["title"]
        assert ctx.store.get_event("ucdp-1") is not None
        cur = ctx.get_cursor("ucdp_candidate")
        assert cur["events"] == 3
        assert cur["files"][f"{BASE}/GEDEvent_v26_0_10.csv"]["etag"] == '"e1"'
        await c.poll(ctx)
    assert newest.calls[1].request.headers["If-None-Match"] == '"e1"'
    assert ytd.calls[1].request.headers["If-Modified-Since"].startswith("Sat, 21 Nov")


@respx.mock
async def test_newest_only_and_failures(tmp_path):
    respx.get(ucdp.DOWNLOADS_URL).mock(return_value=httpx.Response(200, text=PAGE))
    newest = respx.get(f"{BASE}/GEDEvent_v26_0_10.csv").mock(
        return_value=httpx.Response(500))
    ytd = respx.get(f"{BASE}/GEDEvent_v26_01_26_09.csv")
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="all UCDP candidate files failed"):
            await UcdpCandidateCollector({"all_files": False}).poll(_ctx(http, tmp_path))
    assert newest.called and not ytd.called


@respx.mock
async def test_downloads_page_errors(tmp_path):
    respx.get(ucdp.DOWNLOADS_URL).mock(return_value=httpx.Response(200, text="<html></html>"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="no candidate CSV"):
            await UcdpCandidateCollector({}).poll(_ctx(http, tmp_path))
