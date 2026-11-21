"""USNI Fleet Tracker parsing on a synthetic fixture shaped like the real article markup."""

from datetime import UTC, datetime

import httpx
import respx

from osint.naval.usni import fetch_latest, latest_tracker_url, lookup_region, parse_tracker
from tests.naval.helpers import cfg

CATEGORY = """<html><body>
<a href="https://news.usni.org/2026/11/09/usni-news-fleet-and-marine-tracker-nov-9-2026">x</a>
<a href="https://news.usni.org/2026/11/16/usni-news-fleet-and-marine-tracker-nov-16-2026">y</a>
<a href="https://news.usni.org/2026/10/26/usni-news-fleet-and-marine-tracker-oct-26-2026">z</a>
<a href="https://news.usni.org/2026/11/17/some-other-story">n</a>
</body></html>"""

ARTICLE = """<html><body><h2>Search</h2>
<div class="entry-content single56__content">
<p>These are the approximate positions of the U.S. Navy&#8217;s deployed carrier strike groups.</p>
<h2>In Japan</h2>
<p>USS <em>Tripoli</em> (LHA-7) is in port in Sasebo, Japan.</p>
<h2><strong>In the Philippine Sea </strong></h2>
<p>Aircraft carrier USS&nbsp;<em>Carl Vinson</em>&nbsp;(CVN-70) is underway in the Philippine Sea.</p>
<h3>Carrier Strike Group 1</h3>
<p>Carrier USS <em>Carl Vinson</em> (CVN-70), homeported at Naval Air Station North Island.</p>
<h2><strong>In the Arabian Sea</strong></h2>
<p>Aircraft carrier USS <em>George Washington</em> (CVN-73) and USS <em>Shoup</em> (DDG-86) are operating in the <a href="#">Arabian Sea in support of </a>operations.</p>
<p>The George H.W. Bush Carrier Strike Group is operating in the Arabian Sea.</p>
<p>Carrier USS <em>George H. W. Bush</em> (CVN-77), homeported at Naval Station Norfolk, Va.</p>
<h2><strong>In the Western Atlantic</strong></h2>
<p>Aircraft carrier USS <em>Nimitz </em>(CVN-68) returned last Monday to Norfolk, Va.</p>
<h3>U.S. Naval Institute Staff</h3>
<h2>Related Posts</h2>
<p>Aircraft carrier USS <em>Ronald Reagan</em> (CVN-76) is operating in the South China Sea.</p>
</div></body></html>"""


def test_latest_tracker_url():
    url, as_of = latest_tracker_url(CATEGORY)
    assert as_of == "2026-11-16" and url.endswith("nov-16-2026")
    assert latest_tracker_url("<html></html>") is None


def test_region_lookup_prefers_longest_key():
    regions = cfg().sec("usni")["regions"]
    assert lookup_region("operating in the North Arabian Sea", regions)[0] == "north arabian sea"
    assert lookup_region("Arabian Sea", regions)[0] == "arabian sea"
    assert lookup_region("Somewhere unknown", regions) is None


def test_parse_tracker_entries():
    out = parse_tracker(ARTICLE, cfg().sec("usni")["regions"], url="u", as_of="2026-11-16")
    by = {e["carrier"]: e for e in out["entries"]}
    assert set(by) == {"USS Carl Vinson", "USS George Washington", "USS George H. W. Bush",
                       "USS Nimitz"}  # Ronald Reagan sits after the article end marker
    cv = by["USS Carl Vinson"]
    assert cv["hull"] == "CVN-70" and cv["region"] == "Philippine Sea"
    assert cv["status"] == "at_sea" and (cv["lat"], cv["lon"]) == (20.0, 130.0)
    assert by["USS George Washington"]["region_key"] == "arabian sea"
    bush = by["USS George H. W. Bush"]
    assert bush["hull"] == "CVN-77" and bush["status"] == "at_sea"
    nim = by["USS Nimitz"]
    assert nim["status"] == "in_port" and nim["region_key"] == "norfolk"
    assert all("text" not in e for e in out["entries"])   # names + regions only


@respx.mock
async def test_fetch_latest_respects_min_interval():
    c = {**cfg().sec("usni"), "category_url": "https://usni.test/cat"}
    respx.get("https://usni.test/cat").mock(return_value=httpx.Response(200, text=CATEGORY))
    art = respx.get("https://news.usni.org/2026/11/16/usni-news-fleet-and-marine-tracker-nov-16-2026"
                    ).mock(return_value=httpx.Response(200, text=ARTICLE))
    now = datetime(2026, 11, 21, 12, tzinfo=UTC)
    async with httpx.AsyncClient() as http:
        st = await fetch_latest(http, c, None, now)
        assert st["as_of"] == "2026-11-16" and len(st["entries"]) == 4
        assert await fetch_latest(http, c, st, now) is None   # < 12 h later: no request
        later = datetime(2026, 11, 22, 1, tzinfo=UTC)
        st2 = await fetch_latest(http, c, st, later)          # same article: not re-fetched
        assert st2["entries"] == st["entries"] and art.call_count == 1


FEED = ("<rss><channel><item><title>T</title>"
        "<link>https://news.usni.org/2026/11/16/usni-news-fleet-and-marine-tracker-nov-16-2026</link>"
        "<content:encoded><![CDATA[" + ARTICLE + "]]></content:encoded></item>"
        "<item><link>https://news.usni.org/2026/11/09/usni-news-fleet-and-marine-tracker-nov-9-2026"
        "</link><content:encoded><![CDATA[<p>old</p>]]></content:encoded></item></channel></rss>")


@respx.mock
async def test_challenged_category_page_falls_back_to_rss_feed():
    c = {**cfg().sec("usni"), "category_url": "https://usni.test/cat",
         "feed_url": "https://usni.test/feed"}
    respx.get("https://usni.test/cat").mock(return_value=httpx.Response(403, text="challenge"))
    respx.get("https://usni.test/feed").mock(return_value=httpx.Response(200, text=FEED))
    async with httpx.AsyncClient() as http:
        st = await fetch_latest(http, c, None, datetime(2026, 11, 21, tzinfo=UTC))
    assert st["as_of"] == "2026-11-16" and len(st["entries"]) == 4 and "403" in st["via"]
