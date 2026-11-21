import httpx
import pytest

from osint.pipeline.extract.config import FetchConfig
from osint.pipeline.extract.fetch import get_article_text
from osint.pipeline.extract.readability import extract_article

BODY = ("Russian air defences shot down several drones over the Kaliningrad region on "
        "Tuesday night, the regional governor said in a statement on Telegram.")

EN_HTML = f"""<!doctype html><html><head><title>Site | Drones</title>
<meta property="og:title" content="Drones downed over Kaliningrad">
<meta name="description" content="Governor reports attack.">
<script>var x = "SCRIPT TEXT SHOULD NOT APPEAR";</script><style>.a{{color:red}}</style>
</head><body>
<nav><a href="/">Home</a> <a href="/world">World news menu link text here and more</a></nav>
<div class="cookie-consent">We use cookies to improve your experience on this site, ok?</div>
<header><p>Breaking news ticker header that is long enough to count as text.</p></header>
<article><h1>Drones downed</h1>
<p>{BODY}</p><p>No casualties were reported,<br>according to officials who spoke to reporters.</p>
<p>Short.</p><img src="x.png">
<p>The incident follows a series of similar attacks on the Baltic exclave this month.</p>
</article>
<aside><p>Related: some other story that is quite long and should be removed entirely.</p></aside>
<footer><p>Copyright 2026 Example News. All rights reserved. Terms and privacy.</p></footer>
</body></html>"""


def test_extracts_article_and_drops_chrome():
    art = extract_article(EN_HTML)
    assert art.title == "Drones downed over Kaliningrad"
    assert art.description == "Governor reports attack."
    assert BODY in art.text
    assert "No casualties were reported, according to officials" in art.text
    for junk in ("SCRIPT TEXT", "menu link", "cookies", "ticker", "Related:", "Copyright",
                 "Short."):
        assert junk not in art.text
    assert "  " not in art.text and art.body_chars > 200


def test_non_english_and_main_fallback():
    ru = ("Силы ПВО сбили несколько беспилотников над Калининградской областью во вторник "
          "вечером, сообщил губернатор региона.")
    ja = "防衛省によると、火曜日の夜にカリーニングラード上空で無人機が撃墜された。"
    html = (f'<html><head><meta charset="utf-8"></head><body><div class="menu">menu menu '
            f'menu menu menu menu menu menu</div><main><p>{ru}</p><p>{ja}</p>'
            f'<p>{ru} Ещё одна фраза для длины текста статьи здесь.</p></main></body></html>')
    art = extract_article(html)
    assert ru in art.text and ja in art.text and "menu" not in art.text


def test_ld_json_article_body_fallback_and_cap():
    body = "Officials confirmed the blast at the port. " * 20
    html = ('<html><head><script type="application/ld+json">{"@graph":[{"@type":"NewsArticle",'
            f'"headline":"Blast","articleBody":"{body}"}}]}}</script></head>'
            '<body><div id="app"></div></body></html>')
    art = extract_article(html, max_chars=300)
    assert art.text.startswith("Officials confirmed the blast") and len(art.text) == 300


def test_garbage_html_does_not_raise():
    assert extract_article("<p><b>unclosed <i>tags </div></p></span>&amp;").text is not None


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


async def test_fetch_direct_then_snippet_fallback():
    cfg = FetchConfig(min_chars=100)

    def ok(req):
        assert req.headers["user-agent"].startswith("osint-poc/0.1")
        return httpx.Response(200, html=EN_HTML)

    async with _client(ok) as c:
        got = await get_article_text(c, "https://x.test/a", title="T", snippet="S", cfg=cfg)
    assert got.source == "direct" and BODY in got.text

    async with _client(lambda req: httpx.Response(403)) as c:
        got = await get_article_text(c, "https://x.test/a", title="Title", snippet="Snip",
                                     cfg=cfg)
    assert got.source == "snippet" and got.text == "Title\nSnip" and "403" in got.error


async def test_fetch_size_cap_and_alterlab_fallback():
    cfg = FetchConfig(min_chars=100, max_bytes=50)
    md = "# Heading\n\n" + "[Officials](https://l) said the port blast killed two. " * 5
    seen = {}

    def handler(req):
        if req.url.host == "api.alterlab.io":
            seen["key"] = req.headers["x-api-key"]
            seen["body"] = req.content
            return httpx.Response(200, json={"content": {"markdown": md},
                                             "billing": {"total_credits": 2}})
        return httpx.Response(200, html=EN_HTML)  # truncated to 50 bytes -> too little text

    async with _client(handler) as c:
        got = await get_article_text(c, "https://x.test/a", title="T", snippet="S", cfg=cfg,
                                     alterlab_key="k1")
    assert got.source == "alterlab" and got.credits == 2 and seen["key"] == "k1"
    assert b'"max_credits"' in seen["body"] and b'"markdown"' in seen["body"]
    assert "Officials said the port blast killed two." in got.text and "](" not in got.text


@pytest.mark.parametrize("url", ["", "ftp://x"])
async def test_fetch_non_http_url_uses_snippet(url):
    async with _client(lambda req: httpx.Response(500)) as c:
        got = await get_article_text(c, url, title="T", snippet="", cfg=FetchConfig())
    assert got.source == "snippet" and got.text == "T"
