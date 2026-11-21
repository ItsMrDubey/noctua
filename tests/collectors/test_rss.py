import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from osint.collectors.rss import (
    RssCollector,
    UnsafeFeedError,
    items_to_documents,
    parse_date,
    parse_feed,
    strip_html,
)
from osint.core.collector import CollectorContext
from osint.store.sqlite import SqliteStore

FETCHED = datetime(2026, 11, 21, 14, 0, tzinfo=UTC)

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>World</title>
<item><title>Missile strike hits Kharkiv</title>
  <link>https://news.example/a?utm_source=rss</link>
  <description>&lt;p&gt;Russian forces &lt;b&gt;struck&lt;/b&gt; the city.&lt;/p&gt;</description>
  <pubDate>Sat, 21 Nov 2026 13:05:00 GMT</pubDate><guid>a-1</guid></item>
<item><title>No date here</title><link>https://news.example/b</link></item>
<item><title>Guid permalink only</title><guid>https://news.example/c</guid>
  <dc:date>2026-11-21T12:00:00+02:00</dc:date></item>
<item><title></title><link>https://news.example/empty-title</link></item>
<item><title>Bad link</title><link>javascript:alert(1)</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Maritime</title>
<entry><title>Tanker attacked in Red Sea</title>
  <link rel="alternate" href="https://maritime.example/tanker"/>
  <id>urn:x:1</id><updated>2026-11-21T10:00:00Z</updated>
  <summary type="html">UKMTO reports an incident 40nm west of Hodeidah</summary></entry>
<entry><title>Second</title><link href="https://maritime.example/2"/>
  <published>2026-11-21T11:00:00Z</published><content>Body</content></entry>
</feed>"""

RDF = b"""<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel rdf:about="https://dw.example"><title>DW</title></channel>
<item rdf:about="https://dw.example/x"><title>Stromausfall in Berlin</title>
 <link>https://dw.example/x</link><description>Brandanschlag</description>
 <dc:date>2026-11-21T09:30:00Z</dc:date></item>
</rdf:RDF>"""

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">]>
<rss version="2.0"><channel><item><title>&lol2;</title></item></channel></rss>"""

EXTERNAL_ENTITY = b"""<?xml version="1.0"?>
<!doctype rss [<!entity xxe SYSTEM "file:///etc/passwd">]>
<rss version="2.0"><channel><item><title>&xxe;</title></item></channel></rss>"""

FEED = {"url": "https://news.example/rss", "name": "example", "language": "en",
        "country": "GB"}


def test_parse_rss2():
    items = parse_feed(RSS)
    assert len(items) == 5
    assert items[0]["link"] == "https://news.example/a?utm_source=rss"
    assert items[0]["published"] == "Sat, 21 Nov 2026 13:05:00 GMT"
    assert items[2]["link"] == "https://news.example/c"  # permalink guid


def test_items_to_documents_cleans_and_skips(caplog):
    with caplog.at_level(logging.WARNING):
        docs = items_to_documents(parse_feed(RSS), FEED, FETCHED)
    assert [d.url for d in docs] == ["https://news.example/a?utm_source=rss",
                                     "https://news.example/b", "https://news.example/c"]
    a, b, c = docs
    assert a.snippet == "Russian forces struck the city."
    assert a.published_at == datetime(2026, 11, 21, 13, 5, tzinfo=UTC)
    assert a.source == "rss:example" and a.language == "en" and a.origin_id == "a-1"
    assert a.attrs == {"feed": "example", "country": "GB"}
    assert b.published_at == FETCHED and b.attrs["date_missing"] is True
    assert c.published_at == datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
    assert "skipped 2 malformed" in caplog.text


def test_parse_atom_and_rdf():
    atom = parse_feed(ATOM)
    assert atom[0]["link"] == "https://maritime.example/tanker"
    assert atom[0]["published"] == "2026-11-21T10:00:00Z"
    assert atom[1]["summary"] == "Body" and atom[1]["link"] == "https://maritime.example/2"
    rdf = parse_feed(RDF)
    assert rdf == [{"title": "Stromausfall in Berlin", "link": "https://dw.example/x",
                    "summary": "Brandanschlag", "published": "2026-11-21T09:30:00Z",
                    "guid": "https://dw.example/x"}]


@pytest.mark.parametrize("payload", [BILLION_LAUGHS, EXTERNAL_ENTITY])
def test_entity_attacks_rejected(payload):
    with pytest.raises(UnsafeFeedError):
        parse_feed(payload)


def test_non_feed_rejected():
    with pytest.raises(ValueError, match="not an RSS"):
        parse_feed(b"<html><body>hi</body></html>")


def test_helpers():
    assert strip_html("&lt;b&gt;Hi&lt;/b&gt; &amp;amp; there") == "Hi & there"
    assert parse_date("garbage") is None
    assert parse_date("2026-11-21T10:00:00") == datetime(2026, 11, 21, 10, 0, tzinfo=UTC)


def _ctx(http, tmp_path):
    return CollectorContext(http=http, settings=SimpleNamespace(),
                            store=SqliteStore(tmp_path / "s.sqlite"))


@respx.mock
async def test_poll_partial_failure_and_conditional_get(tmp_path, caplog):
    ok = respx.get("https://news.example/rss").mock(side_effect=[
        httpx.Response(200, content=RSS, headers={"ETag": '"v1"',
                                                  "Last-Modified": "Sat, 21 Nov 2026"}),
        httpx.Response(304),
    ])
    respx.get("https://maritime.example/atom").mock(return_value=httpx.Response(200, content=ATOM))
    respx.get("https://broken.example/rss").mock(return_value=httpx.Response(500))
    respx.get("https://evil.example/rss").mock(
        return_value=httpx.Response(200, content=BILLION_LAUGHS))
    cfg = {"feeds": [FEED, {"url": "https://maritime.example/atom", "name": "maritime"},
                     {"url": "https://broken.example/rss", "name": "broken"},
                     {"url": "https://evil.example/rss", "name": "evil"}]}
    c = RssCollector(cfg)
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http, tmp_path)
        with caplog.at_level(logging.WARNING):
            batch = await c.poll(ctx)
        assert len(batch.documents) == 5
        assert {d.source for d in batch.documents} == {"rss:example", "rss:maritime"}
        assert "broken: RuntimeError: HTTP 500" in caplog.text
        assert "evil: UnsafeFeedError" in caplog.text
        cursor = ctx.get_cursor("rss")
        assert cursor["failed"] == 2
        assert cursor["feeds"]["https://news.example/rss"]["etag"] == '"v1"'
        second = await c.poll(ctx)
    assert ok.calls[1].request.headers["If-None-Match"] == '"v1"'
    assert ok.calls[1].request.headers["If-Modified-Since"] == "Sat, 21 Nov 2026"
    assert all(d.source != "rss:example" for d in second.documents)  # 304 -> nothing
    assert (c.name, c.commercial_ok) == ("rss", False)


@respx.mock
async def test_poll_raises_when_all_feeds_fail(tmp_path):
    respx.get("https://news.example/rss").mock(side_effect=httpx.ConnectError("down"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="all RSS feeds failed"):
            await RssCollector({"feeds": [FEED]}).poll(_ctx(http, tmp_path))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="no feeds"):
            await RssCollector({}).poll(_ctx(http, tmp_path))


def test_repo_feed_list_is_well_formed():
    from osint.config import ROOT, load_sources

    feeds = load_sources(ROOT / "config")["rss"]["feeds"]
    assert len(feeds) >= 25
    assert all(f["url"].startswith("https://") and f["name"] and f["language"] for f in feeds)
    assert len({f["name"] for f in feeds}) == len(feeds)
    langs = {f["language"] for f in feeds}
    assert {"en", "ar", "ru", "uk", "fa", "fr", "es", "de", "zh", "tr"} <= langs
