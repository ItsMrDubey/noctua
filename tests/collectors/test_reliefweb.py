from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx

from osint.collectors.reliefweb import API_URL, ReliefWebCollector, parse_disaster, parse_report

NOW = datetime(2026, 11, 23, 4, 0, tzinfo=UTC)
# Shapes from the ReliefWeb API documentation (v2 = v1 response format). The live API could
# not be sampled: it needs an approved appname (403 otherwise, checked 2026-11-23).
DISASTER = {"id": "52371", "score": 1, "fields": {
    "id": 52371, "name": "Philippines: Floods and Landslides - Nov 2026",
    "glide": "FL-2026-000210-PHL", "status": "current",
    "date": {"created": "2026-11-18T00:00:00+00:00", "changed": "2026-11-22T09:12:00+00:00",
             "event": "2026-11-17T00:00:00+00:00"},
    "primary_country": {"name": "Philippines", "iso3": "phl",
                        "location": {"lat": 11.75, "lon": 122.88}},
    "country": [{"name": "Philippines", "iso3": "phl"}],
    "primary_type": {"code": "FL", "name": "Flood"},
    "url_alias": "https://reliefweb.int/disaster/fl-2026-000210-phl"}}
REPORT = {"id": "4100001", "fields": {
    "id": 4100001, "title": "Philippines: Floods - Flash Update No. 2 (22 November 2026)",
    "url_alias": "https://reliefweb.int/report/philippines/philippines-floods-flash-update-2",
    "date": {"created": "2026-11-22T10:00:00+00:00", "original": "2026-11-22T08:00:00+00:00"},
    "source": [{"name": "UN Office for the Coordination of Humanitarian Affairs",
                "shortname": "OCHA"}],
    "primary_country": {"name": "Philippines", "iso3": "phl"},
    "country": [{"name": "Philippines", "iso3": "phl"}],
    "disaster": [{"name": "Philippines: Floods and Landslides - Nov 2026"}],
    "disaster_type": [{"name": "Flood"}], "language": [{"code": "en"}],
    "format": [{"name": "Situation Report"}]}}


class Ctx(SimpleNamespace):
    cursor = None

    def get_cursor(self, name):
        return self.cursor

    def set_cursor(self, name, cursor):
        self.cursor = cursor


def test_parse_disaster_country_level_hazard():
    o = parse_disaster(DISASTER, NOW)
    assert o.id == "hazard:reliefweb:52371" and o.type == "hazard"
    assert (o.lat, o.lon) == (11.75, 122.88) and o.source == "reliefweb"
    a = o.attrs
    assert a["hazard"] == "flood" and a["precision"] == "country" and a["iso3"] == "PHL"
    assert a["iso3s"] == ["PHL"] and a["glide"] == "FL-2026-000210-PHL" and a["current"]
    assert a["t_start"] == "2026-11-17T00:00:00+00:00"
    assert o.ts == datetime(2026, 11, 22, 9, 12, tzinfo=UTC)
    no_loc = {"fields": {**DISASTER["fields"], "primary_country": {"name": "X"}}}
    assert parse_disaster(no_loc, NOW) is None


def test_parse_report_metadata_only():
    d = parse_report(REPORT, NOW)
    assert d.source == "reliefweb" and d.kind == "article" and d.language == "en"
    assert d.url.startswith("https://reliefweb.int/report/") and d.origin_id == "4100001"
    assert d.snippet == ("OCHA · Situation Report · Philippines · "
                         "Philippines: Floods and Landslides - Nov 2026")
    assert d.published_at == datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
    assert parse_report(REPORT, NOW + timedelta(days=3)) is None  # too old for ingest


async def test_poll_without_appname_fails_clearly():
    with pytest.raises(RuntimeError, match="appname"):
        await ReliefWebCollector({}).poll(Ctx(http=None, settings=SimpleNamespace(
            reliefweb_appname="")))


@respx.mock
async def test_poll_disasters_and_reports_with_cursor():
    dis = respx.get(f"{API_URL}/disasters").mock(
        return_value=httpx.Response(200, json={"totalCount": 1, "count": 1,
                                               "data": [DISASTER]}))
    rep = respx.get(f"{API_URL}/reports").mock(
        return_value=httpx.Response(200, json={"count": 1, "data": [REPORT]}))
    async with httpx.AsyncClient() as http:
        ctx = Ctx(http=http, settings=SimpleNamespace(reliefweb_appname="approved-app"))
        c = ReliefWebCollector({})
        c_now = datetime.now(UTC)
        report = {"fields": {**REPORT["fields"], "date": {"created": c_now.isoformat()}}}
        rep.mock(return_value=httpx.Response(200, json={"data": [report]}))
        batch = await c.poll(ctx)
    assert len(batch.observations) == 1 and len(batch.documents) == 1
    q = dis.calls[0].request.url.params
    assert q["appname"] == "approved-app" and q.get_list("filter[value][]") == [
        "alert", "current", "ongoing"]
    assert "primary_country" in q.get_list("fields[include][]")
    assert rep.calls[0].request.url.params["filter[field]"] == "date.created"
    assert ctx.cursor["reports_since"].startswith(c_now.date().isoformat())


@respx.mock
async def test_poll_rejected_appname():
    respx.get(f"{API_URL}/disasters").mock(return_value=httpx.Response(403, json={
        "error": {"message": "You are not using an approved appname."}}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="403"):
            await ReliefWebCollector({"appname": "x"}).poll(Ctx(http=http, settings=None))


@respx.mock
async def test_poll_firewall_block_and_user_agent():
    route = respx.get(f"{API_URL}/disasters").mock(return_value=httpx.Response(406, json={
        "error": "Blocked due to bot activity."}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="406"):
            await ReliefWebCollector({"appname": "x"}).poll(Ctx(http=http, settings=None))
    ua = route.calls[0].request.headers["User-Agent"]
    assert "osint" not in ua.lower() and ua.startswith("noctua/")
