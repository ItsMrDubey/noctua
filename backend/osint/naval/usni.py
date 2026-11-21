"""USNI News Fleet and Marine Tracker: carrier names + stated regions (calibration only).

Only the carrier name, hull number, stated region text, a coarse region centroid from the
config table and the article link are kept - never the article text or the map image.
Fetched at most once per ``min_interval_h`` (default 12 h).
"""

import html as htmllib
import logging
import re
from datetime import UTC, datetime, timedelta

from osint.core.geo import utcnow

log = logging.getLogger(__name__)

_LINK = re.compile(
    r'href="(https://news\.usni\.org/(\d{4})/(\d{2})/(\d{2})/'
    r'usni-news-fleet-and-marine-tracker[^"#?]*)"')
_BLOCK = re.compile(r"<(h2|h3|h4|p)\b[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_HULL = re.compile(r"USS\s+([A-Z][A-Za-z.\s]+?)\s*\((CVN[-\s]?\d+)\)")
_CSG_NAME = re.compile(r"(?:The\s+)?([A-Z][A-Za-z.\s]+?)\s+Carrier Strike Group\s+(?:is|was|has)")
_STATUS = re.compile(r"\b(operating|underway|in port|pierside|moored|returned|arrived|departed|"
                     r"transit|is in|are in|is now|sailing|steaming|conducting)\b", re.IGNORECASE)
_IN_PORT = re.compile(r"\b(in port|pierside|moored|returned|arrived|homecoming)\b", re.IGNORECASE)
_END = ("Naval Institute Staff", "Related Topics", "Related Posts", "Get USNI News updates")


def _text(fragment: str) -> str:
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def latest_tracker_url(category_html: str) -> tuple[str, str] | None:
    """(url, YYYY-MM-DD) of the newest tracker article linked from the category page."""
    best = None
    for m in _LINK.finditer(category_html or ""):
        key = f"{m.group(2)}-{m.group(3)}-{m.group(4)}"
        if best is None or key > best[1]:
            best = (m.group(1), key)
    return best


def _norm_name(name: str) -> str:
    return " ".join(name.replace(". ", ".").replace(".", ". ").split()).replace(" .", ".").strip()


def lookup_region(text: str, regions: dict) -> tuple[str, list[float]] | None:
    """Longest region-table key contained in ``text`` (lower case)."""
    t = " " + " ".join(text.lower().replace(",", " ").replace(".", " ").split()) + " "
    hits = [k for k in regions if f" {k} " in t]
    if not hits:
        return None
    k = max(hits, key=len)
    return k, list(regions[k])


def parse_tracker(article_html: str, regions: dict, url: str = "",
                  as_of: str | None = None) -> dict:
    """{url, as_of, entries:[{carrier, hull, region, region_key, status, lat, lon}]}."""
    body = article_html or ""
    i = body.find("entry-content")
    if i >= 0:
        body = body[i:]
    cut = min((body.find(e) for e in _END if body.find(e) > 0), default=-1)
    if cut > 0:
        body = body[:cut]
    region = None
    entries: dict[str, dict] = {}
    for m in _BLOCK.finditer(body):
        tag, text = m.group(1).lower(), _text(m.group(2))
        if not text:
            continue
        if tag == "h2":
            if text.lower().startswith("in "):
                region = re.sub(r"^in\s+(the\s+)?", "", text, flags=re.IGNORECASE).strip()
            continue
        if tag != "p":
            continue
        found: list[tuple[str, str]] = [(_norm_name(n), h.replace(" ", "-").upper())
                                        for n, h in _HULL.findall(text)]
        found += [(_norm_name(n), "") for n in _CSG_NAME.findall(text)]
        for name, hull in found:
            key = hull or name.lower()
            # one carrier can be named by hull or by name; merge on the name
            existing = next((e for e in entries.values()
                             if e["carrier"].lower() == ("USS " + name).lower()), None)
            if existing is not None and (existing["status_line"] or not _STATUS.search(text)):
                if hull and not existing["hull"]:
                    existing["hull"] = hull
                continue
            if "homeported" in text.lower() and not _STATUS.search(text) and existing is not None:
                continue
            spot = lookup_region(text, regions) if _STATUS.search(text) else None
            if spot is None and region:
                spot = lookup_region(region, regions)
            status = "in_port" if _IN_PORT.search(text) and not re.search(
                r"\bdeparted\b", text, re.IGNORECASE) else "at_sea"
            entry = {
                "carrier": "USS " + name, "hull": hull, "region": region or "",
                "region_key": spot[0] if spot else None, "status": status,
                "lat": spot[1][0] if spot else None, "lon": spot[1][1] if spot else None,
                "status_line": bool(_STATUS.search(text)),
            }
            if existing is not None:
                entries = {k: v for k, v in entries.items() if v is not existing}
            entries[key] = entry
    out = []
    for e in entries.values():
        e.pop("status_line", None)
        out.append(e)
    return {"url": url, "as_of": as_of, "entries": out}


async def fetch_latest(http, cfg: dict, state: dict | None, now: datetime | None = None
                       ) -> dict | None:
    """Fetch + parse the newest tracker unless ``state`` (previous result) is younger than
    min_interval_h. Returns the new state, or None when nothing was fetched."""
    now = now or utcnow()
    if not cfg.get("enabled", True):
        return None
    min_h = float(cfg.get("min_interval_h", 12))
    if state and state.get("fetched_at"):
        try:
            last = datetime.fromisoformat(state["fetched_at"])
            if now - last < timedelta(hours=min_h):
                return None
        except ValueError:
            pass
    cat = cfg.get("category_url", "https://news.usni.org/category/fleet-tracker")
    r = await http.get(cat, timeout=30)
    if r.status_code in (403, 429, 503):
        # bot challenge / throttled: do not try to get around it; use the public RSS feed
        # (it carries the article HTML, so no second request is made)
        return await _from_feed(http, cfg, now, f"category page HTTP {r.status_code}")
    r.raise_for_status()
    hit = latest_tracker_url(r.text)
    if hit is None:
        raise ValueError("no fleet tracker article linked from the category page")
    url, as_of = hit
    if state and state.get("url") == url and state.get("entries"):
        return {**state, "fetched_at": now.astimezone(UTC).isoformat()}
    a = await http.get(url, timeout=30)
    a.raise_for_status()
    parsed = parse_tracker(a.text, cfg.get("regions") or {}, url=url, as_of=as_of)
    parsed["fetched_at"] = now.astimezone(UTC).isoformat()
    log.info("usni: %s -> %d carrier entries", url, len(parsed["entries"]))
    return parsed


_ITEM = re.compile(r"<item>(.*?)</item>", re.DOTALL)


def parse_feed(feed_xml: str, regions: dict) -> dict | None:
    """Newest fleet-tracker item of the category RSS feed, parsed from content:encoded."""
    best = None
    for m in _ITEM.finditer(feed_xml or ""):
        item = m.group(1)
        raw = re.search(r"<link>(.*?)</link>", item, re.DOTALL)
        link = _LINK.search(f'href="{raw.group(1).strip()}"') if raw else None
        body = re.search(r"<content:encoded><!\[CDATA\[(.*?)\]\]></content:encoded>", item, re.DOTALL)
        if not link or not body:
            continue
        key = f"{link.group(2)}-{link.group(3)}-{link.group(4)}"
        if best is None or key > best[1]:
            best = (link.group(1), key, body.group(1))
    if best is None:
        return None
    return parse_tracker(best[2], regions, url=best[0], as_of=best[1])


async def _from_feed(http, cfg: dict, now: datetime, why: str) -> dict:
    feed = cfg.get("feed_url", "https://news.usni.org/category/fleet-tracker/feed")
    r = await http.get(feed, timeout=30)
    r.raise_for_status()
    parsed = parse_feed(r.text, cfg.get("regions") or {})
    if parsed is None:
        raise ValueError(f"{why}; no tracker item in the RSS feed")
    parsed["fetched_at"] = now.astimezone(UTC).isoformat()
    parsed["via"] = f"rss feed ({why})"
    log.info("usni: %s via feed -> %d carrier entries", parsed["url"], len(parsed["entries"]))
    return parsed
