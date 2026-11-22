import asyncio

import httpx
import pytest

from osint.browse import BlockedURL, Browser, check_url, frameable, page_meta

PUBLIC = lambda host: True  # noqa: E731 - test resolver: every host is public

ARTICLE = """<html><head><title>T</title>
<meta property="og:title" content="Strike hits depot">
<meta property="og:site_name" content="Example News">
<meta property="og:image" content="/img/hero.jpg">
<meta property="article:published_time" content="2026-11-22T10:00:00Z">
</head><body><article>
<p>First paragraph of the report with enough words to count as body text here.</p>
<p>Second paragraph describing the damage at the depot in some useful detail.</p>
<p>Third paragraph quoting an official about the cause and the response.</p>
</article></body></html>"""


def test_check_url_rejects_non_public():
    with pytest.raises(BlockedURL):
        check_url("file:///etc/passwd", PUBLIC)
    with pytest.raises(BlockedURL):
        check_url("http://localhost:8765/health", PUBLIC)
    with pytest.raises(BlockedURL):
        check_url("http://user:pw@example.com/", PUBLIC)
    with pytest.raises(BlockedURL):
        check_url("http://intranet.example/", lambda h: False)
    assert check_url(" https://example.com/a ", PUBLIC) == "https://example.com/a"


def test_real_resolver_blocks_loopback_and_private():
    with pytest.raises(BlockedURL):
        check_url("http://127.0.0.1/")
    with pytest.raises(BlockedURL):
        check_url("http://10.0.0.1/")


def test_frameable_headers():
    assert frameable(httpx.Headers({}))
    assert not frameable(httpx.Headers({"x-frame-options": "SAMEORIGIN"}))
    assert not frameable(httpx.Headers({"content-security-policy": "frame-ancestors 'self'"}))
    assert frameable(httpx.Headers({"content-security-policy": "script-src 'self'; frame-ancestors *"}))


def test_page_meta_first_value_wins():
    m = page_meta(ARTICLE)
    assert m["og:title"] == "Strike hits depot" and m["og:site_name"] == "Example News"


def _browser(handler):
    return Browser(httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolve_host=PUBLIC)


def test_read_extracts_article_and_follows_checked_redirects():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, html=ARTICLE, headers={"x-frame-options": "DENY"})

    out = asyncio.run(_browser(handler).read("https://news.example/old"))
    assert out["final_url"] == "https://news.example/new"
    assert out["title"] == "Strike hits depot" and out["site"] == "Example News"
    assert out["image"] == "https://news.example/img/hero.jpg"
    assert len(out["paragraphs"]) == 3 and out["frameable"] is False and out["via"] == "direct"
    assert seen == ["https://news.example/old", "https://news.example/new"]


def test_redirect_to_private_host_is_refused():
    def handler(req):
        return httpx.Response(302, headers={"location": "http://localhost/admin"})

    with pytest.raises(BlockedURL):
        asyncio.run(_browser(handler).read("https://news.example/x"))


def test_api_route_maps_errors():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from osint.api.browse import mount_browse

    app = FastAPI()
    mount_browse(app, _browser(lambda req: httpx.Response(200, html=ARTICLE)))
    c = TestClient(app)
    assert c.get("/api/browse/read", params={"url": "ftp://x"}).status_code == 400
    r = c.get("/api/browse/read", params={"url": "https://news.example/a"})
    assert r.status_code == 200 and r.json()["title"] == "Strike hits depot"
