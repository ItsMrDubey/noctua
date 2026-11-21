import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from osint.collectors.news_alterlab import API_URL, AlterLabNewsCollector, parse_item
from osint.core.collector import CollectorContext
from osint.store.sqlite import SqliteStore

FETCHED = datetime(2026, 11, 21, 14, 0, tzinfo=UTC)

ITEM = {
    "headline": "US pursuit of 'Trump-class' warships has blown cost of new fleet",
    "url": "https://www.theguardian.com/us-news/2026/nov/21/trump-class-warships",
    "source": "guardian-world", "source_name": "The Guardian",
    "published_at": "2026-11-21T13:00:00Z", "author": "Ben Doherty", "image": None,
    "snippet": "Iran war stretching fleet's capacity, navy chief admits",
    "category": "world", "language": "en",
}


def body(items):
    return {"object": "news_list", "count": len(items), "items": items,
            "attribution": {"text": "News via AlterLab", "url": "https://alterlab.io/news-api",
                            "required": True},
            "rate_limit": {"tier": "keyless", "limit_per_hour": 60}}


def test_parse_item():
    d = parse_item(ITEM, FETCHED)
    assert d.source == "news_alterlab" and d.kind == "article"
    assert d.published_at == datetime(2026, 11, 21, 13, 0, tzinfo=UTC)
    assert d.attrs["attribution"] == "News via AlterLab"
    assert d.attrs["publisher"] == "The Guardian" and d.language == "en"
    assert parse_item({**ITEM, "url": "ftp://x"}, FETCHED) is None
    assert parse_item({**ITEM, "published_at": "yesterday"}, FETCHED) is None
    assert parse_item({**ITEM, "headline": ""}, FETCHED) is None
    assert parse_item("not a dict", FETCHED) is None
    long = parse_item({**ITEM, "snippet": "x " * 400}, FETCHED)
    assert len(long.snippet) <= 300


def _ctx(http, tmp_path, key=""):
    return CollectorContext(http=http, settings=SimpleNamespace(alterlab_api_key=key),
                            store=SqliteStore(tmp_path / "s.sqlite"))


@respx.mock
async def test_poll_categories_cursor_key_and_partial_failure(tmp_path, caplog):
    world = {**ITEM}
    dup_in_business = {**ITEM, "category": "business"}
    later = {**ITEM, "url": "https://e.x/later", "published_at": "2026-11-21T13:30:00Z"}

    def handler(request):
        cat = request.url.params["category"]
        if cat == "world":
            return httpx.Response(200, json=body([world, later, {"junk": 1}]))
        if cat == "business":
            return httpx.Response(200, json=body([dup_in_business]))
        if cat == "science":
            return httpx.Response(429, text="rate limited for key SECRETKEY")
        return httpx.Response(200, json=body([]))

    route = respx.get(API_URL).mock(side_effect=handler)
    c = AlterLabNewsCollector({})
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http, tmp_path, key="SECRETKEY")
        with caplog.at_level(logging.WARNING):
            batch = await c.poll(ctx)
        assert len(batch.documents) == 2  # same article in two categories -> one document
        cursor = ctx.get_cursor("news_alterlab")["since"]
        assert cursor["world"] == "2026-11-21T13:30:00Z"
        assert "science" not in cursor or cursor["science"] != "2026-11-21T13:30:00Z"
        await c.poll(ctx)
    first = route.calls[0].request
    assert first.headers["X-API-Key"] == "SECRETKEY"
    assert first.url.params["limit"] == "100"
    world_calls = [call.request for call in route.calls
                   if call.request.url.params["category"] == "world"]
    assert world_calls[1].url.params["since"] == "2026-11-21T13:30:00Z"
    assert "science: HTTP 429" in caplog.text
    assert "SECRETKEY" not in caplog.text  # key redacted
    assert "skipped 1 malformed" in caplog.text
    assert (c.name, c.commercial_ok) == ("news_alterlab", True)


@respx.mock
async def test_poll_keyless_and_all_fail(tmp_path):
    route = respx.get(API_URL).mock(return_value=httpx.Response(200, text="<html>blocked"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="all AlterLab news categories failed"):
            await AlterLabNewsCollector({"categories": ["world"]}).poll(_ctx(http, tmp_path))
    assert "X-API-Key" not in route.calls[0].request.headers
    respx.get(API_URL).mock(return_value=httpx.Response(200, json={"items": "nope"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="no items list"):
            await AlterLabNewsCollector({"categories": ["world"]}).poll(_ctx(http, tmp_path))
