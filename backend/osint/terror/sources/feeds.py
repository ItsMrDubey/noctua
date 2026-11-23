"""Justice / law-enforcement press releases and counter-terrorism research feeds.

* US DOJ press releases (justice.gov JSON API): kept when DOJ itself tags the release with a
  terrorism topic (Counterterrorism, Domestic Terrorism, Terrorism) or when it comes from
  the National Security Division and names terrorism. The RSS feeds ignore their topic
  filters, so the API is used.
* Europol newsroom RSS: kept when the title/summary names terrorism or violent extremism.
  The feed carries no dates: items are dated by first sight (labelled as such).
* Research (GNET; LWJ and Crisis Group already arrive through the RSS collector): analysis
  context only, never an incident source.
"""

from __future__ import annotations

import html as htmllib
import json
import re
from datetime import UTC, datetime

from osint.collectors.rss import parse_date, parse_feed
from osint.terror import text as rules

DOJ_URL = ("https://www.justice.gov/api/v1/press_releases.json?pagesize=50&sort=date"
           "&direction=DESC&page={page}")
EUROPOL_URL = "https://www.europol.europa.eu/rss.xml"
GNET_URL = "https://gnet-research.org/feed/"
DOJ_TOPICS = {"counterterrorism", "domestic terrorism", "terrorism", "counter terrorism"}
_TERROR = re.compile(r"\bterror\w*|\bISIS\b|\bISIL\b|Islamic State|al[- ]Qa'?[ie]da|"
                     r"material support|Hizballah|Hezbollah|Hamas|\bextremis\w*|jihad\w*|"
                     r"Foreign Terrorist Organization|\bFTO\b|\bSDGT\b", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _clean(s: str | None, limit: int = 400) -> str:
    return _WS.sub(" ", htmllib.unescape(_TAG.sub(" ", s or ""))).strip()[:limit]


def _names(raw) -> list[str]:
    """DOJ API fields are lists of dicts, or their Python repr as a string."""
    if isinstance(raw, list):
        return [str(x.get("name")) for x in raw if isinstance(x, dict) and x.get("name")]
    if isinstance(raw, str):
        return re.findall(r"'name':\s*'([^']*)'", raw) + re.findall(r'"name":\s*"([^"]*)"', raw)
    return []


def doc_kind(title: str, summary: str) -> str:
    """prosecution | arrest | plot | designation | other (deterministic, from the text)."""
    t = f"{title} {summary}"
    if rules.PROSECUTION.search(t):
        return "prosecution"
    if rules.PLOT.search(t):
        return "plot"
    if rules.ARREST.search(t):
        return "arrest"
    if re.search(r"\bdesignat\w+|\bsanction\w*", t, re.IGNORECASE):
        return "designation"
    return "other"


def parse_doj(body: bytes) -> list[dict]:
    data = json.loads(body)
    out = []
    for r in data.get("results") or []:
        title = _clean(r.get("title"), 300)
        teaser = _clean(r.get("teaser") or r.get("body"), 500)
        topics = _names(r.get("topic"))
        comps = _names(r.get("component"))
        tagged = any(t.casefold() in DOJ_TOPICS for t in topics)
        nsd = any("national security division" in c.casefold() for c in comps)
        named = bool(_TERROR.search(f"{title} {teaser}"))
        if not (tagged or (nsd and named) or (named and "National Security" in topics)):
            continue
        ts = r.get("date")
        try:
            published = datetime.fromtimestamp(int(ts), UTC).isoformat() if ts else None
        except (TypeError, ValueError):
            published = None
        basis = ("DOJ topic tag: " + ", ".join(t for t in topics if t.casefold() in DOJ_TOPICS)
                 if tagged else "NSD release naming terrorism" if nsd else
                 "National Security topic + terrorism named in text")
        out.append({"source": "doj", "id": "doj:" + str(r.get("uuid") or r.get("url")),
                    "title": title, "summary": teaser, "url": r.get("url"),
                    "published_at": published, "date_basis": "DOJ release date",
                    "topics": topics, "components": comps,
                    "kind": doc_kind(title, teaser), "basis": basis})
    return out


def parse_europol(body: bytes, now: datetime) -> list[dict]:
    out = []
    for it in parse_feed(body):
        title = _clean(it.get("title"), 300)
        summary = _clean(it.get("summary") or it.get("description"), 500)
        if not _TERROR.search(f"{title} {summary}"):
            continue
        pub = parse_date(it.get("published") or "")
        out.append({"source": "europol", "id": "europol:" + (it.get("link") or title),
                    "title": title, "summary": summary, "url": it.get("link"),
                    "published_at": pub.isoformat() if isinstance(pub, datetime) else None,
                    "first_seen": now.isoformat(timespec="seconds"),
                    "date_basis": "feed date" if pub else "first seen (the feed has no dates)",
                    "kind": doc_kind(title, summary), "basis": "terrorism named in text"})
    return out


def parse_research(body: bytes, source: str) -> list[dict]:
    out = []
    for it in parse_feed(body):
        pub = parse_date(it.get("published") or "")
        out.append({"source": source, "id": f"{source}:" + (it.get("link") or it.get("title")),
                    "title": _clean(it.get("title"), 300),
                    "summary": _clean(it.get("summary") or it.get("description"), 400),
                    "url": it.get("link"),
                    "published_at": pub.isoformat() if isinstance(pub, datetime) else None,
                    "date_basis": "feed date", "kind": "analysis",
                    "basis": "research feed (analysis context, not an incident source)"})
    return out
