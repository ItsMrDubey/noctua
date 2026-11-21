"""Curated multilingual RSS / Atom feeds (metadata + links only).

Feeds come from ``config/sources.yaml`` (``rss.feeds: [{url, name, language, country}]``).
They are fetched concurrently with per-feed error isolation and conditional GET
(ETag / Last-Modified, kept per feed in the cursor). Parsing uses xml.etree; since
defusedxml is not available, any document declaring a DTD or entity is rejected outright
(blocks billion-laughs / external-entity tricks).
"""

import asyncio
import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.geo import utcnow
from osint.core.models import Batch, Document, clip_snippet, document_id

log = logging.getLogger(__name__)

MAX_FEED_BYTES = 5 * 1024 * 1024
DEFAULT_CONCURRENCY = 8
_UNSAFE = re.compile(rb"<!DOCTYPE|<!ENTITY", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "rss1": "http://purl.org/rss/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


class UnsafeFeedError(ValueError):
    pass


def _text(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def strip_html(text: str) -> str:
    # unescape twice: feeds often double-encode HTML inside <description>
    text = html.unescape(_TAG.sub(" ", html.unescape(text)))
    return " ".join(_TAG.sub(" ", text).split())


def parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)  # RFC 822 (RSS 2.0)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(value)  # RFC 3339 (Atom, dc)
        except ValueError:
            return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=UTC)  # undated zone: assume UTC rather than drop
    return dt.astimezone(UTC)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _atom_link(entry: ET.Element) -> str:
    links = entry.findall("atom:link", _NS)
    for link in links:
        if link.get("rel", "alternate") == "alternate" and link.get("href"):
            return link.get("href", "")
    return links[0].get("href", "") if links else ""


def _permalink_guid(item: ET.Element) -> str:
    guid = item.find("guid")
    if guid is None or guid.get("isPermaLink", "true") == "false":
        return ""
    return _text(guid)


def parse_feed(data: bytes) -> list[dict[str, Any]]:
    """Items as dicts {title, link, summary, published, guid}. Raises on unsafe/invalid XML."""
    if _UNSAFE.search(data):
        raise UnsafeFeedError("feed declares a DTD/entity; rejected")
    root = ET.fromstring(data)
    kind = _local(root.tag).lower()
    items: list[dict[str, Any]] = []
    if kind == "feed":  # Atom
        for e in root.findall("atom:entry", _NS):
            summary = e.find("atom:summary", _NS)
            if summary is None:
                summary = e.find("atom:content", _NS)
            items.append({
                "title": _text(e.find("atom:title", _NS)),
                "link": _atom_link(e),
                "summary": _text(summary),
                "published": _text(e.find("atom:published", _NS))
                or _text(e.find("atom:updated", _NS)),
                "guid": _text(e.find("atom:id", _NS)),
            })
    elif kind == "rss":  # RSS 2.0
        for e in root.iter("item"):
            items.append({
                "title": _text(e.find("title")),
                "link": _text(e.find("link")) or _permalink_guid(e),
                "summary": _text(e.find("description"))
                or _text(e.find("content:encoded", _NS)),
                "published": _text(e.find("pubDate")) or _text(e.find("dc:date", _NS)),
                "guid": _text(e.find("guid")),
            })
    elif kind == "rdf":  # RSS 1.0
        for e in root.findall("rss1:item", _NS):
            items.append({
                "title": _text(e.find("rss1:title", _NS)),
                "link": _text(e.find("rss1:link", _NS)),
                "summary": _text(e.find("rss1:description", _NS)),
                "published": _text(e.find("dc:date", _NS)),
                "guid": e.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", ""),
            })
    else:
        raise ValueError(f"not an RSS/Atom document (root <{_local(root.tag)}>)")
    return items


def items_to_documents(items: list[dict[str, Any]], feed: dict[str, Any],
                       fetched_at: datetime) -> list[Document]:
    name = str(feed.get("name") or feed["url"])
    out: list[Document] = []
    skipped = 0
    for it in items:
        try:
            link = str(it.get("link") or "").strip()
            title = strip_html(str(it.get("title") or ""))
            if not link.startswith(("http://", "https://")) or not title:
                skipped += 1
                continue
            published = parse_date(str(it.get("published") or ""))
            attrs: dict[str, object] = {"feed": name, "country": feed.get("country") or ""}
            if published is None:
                published, attrs["date_missing"] = fetched_at, True
            out.append(Document(
                id=document_id(link), source=f"rss:{name}", url=link,
                title=clip_snippet(title), snippet=clip_snippet(strip_html(
                    str(it.get("summary") or ""))),
                language=str(feed.get("language") or ""), published_at=published,
                fetched_at=fetched_at, kind="article",
                origin_id=str(it.get("guid") or "")[:300], attrs=attrs,
            ))
        except (ValueError, TypeError, KeyError):
            skipped += 1
    if skipped:
        log.warning("rss %s: skipped %d malformed item(s)", name, skipped)
    return out


@register
class RssCollector(PollCollector):
    name = "rss"
    domain = "news"
    licence = "publisher RSS — metadata + links only"
    commercial_ok = False
    default_interval_s = 600.0

    async def _fetch(self, ctx: CollectorContext, feed: dict[str, Any],
                     state: dict[str, str], sem: asyncio.Semaphore
                     ) -> tuple[list[Document], dict[str, str]]:
        url = feed["url"]
        headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9,"
                             " text/xml;q=0.8, */*;q=0.5"}
        if state.get("etag"):
            headers["If-None-Match"] = state["etag"]
        if state.get("last_modified"):
            headers["If-Modified-Since"] = state["last_modified"]
        async with sem:
            for attempt in range(2):  # one quick retry for transient connect failures
                try:
                    r = await ctx.http.get(url, headers=headers, timeout=30)
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                    if attempt:
                        raise RuntimeError(f"{type(e).__name__}") from None
                    await asyncio.sleep(1.0)
                except httpx.HTTPError as e:
                    raise RuntimeError(f"{type(e).__name__}") from None
        if r.status_code == 304:
            return [], state
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        data = r.content
        if len(data) > MAX_FEED_BYTES:
            raise RuntimeError(f"feed too large ({len(data)} bytes)")
        fetched = utcnow()
        items = await asyncio.to_thread(parse_feed, data)
        new_state = {k: v for k, v in (("etag", r.headers.get("etag", "")),
                                       ("last_modified", r.headers.get("last-modified", "")))
                     if v}
        return items_to_documents(items, feed, fetched), new_state

    async def poll(self, ctx: CollectorContext) -> Batch:
        feeds = [f for f in (self.cfg.get("feeds") or []) if isinstance(f, dict) and f.get("url")]
        if not feeds:
            raise RuntimeError("rss: no feeds configured")
        cursor = ctx.get_cursor(self.name) or {}
        states: dict[str, dict[str, str]] = dict(cursor.get("feeds") or {})
        sem = asyncio.Semaphore(int(self.cfg.get("concurrency", DEFAULT_CONCURRENCY)))
        results = await asyncio.gather(
            *(self._fetch(ctx, f, states.get(f["url"], {}), sem) for f in feeds),
            return_exceptions=True,
        )
        batch = Batch(source=self.name)
        errors: list[str] = []
        for feed, res in zip(feeds, results, strict=True):
            label = feed.get("name") or feed["url"]
            if isinstance(res, BaseException):
                if isinstance(res, asyncio.CancelledError):
                    raise res
                errors.append(f"{label}: {type(res).__name__}: {res}"[:200])
                continue
            docs, state = res
            batch.documents.extend(docs)
            states[feed["url"]] = state
        for err in errors:
            log.warning("rss feed failed: %s", err)
        if len(errors) == len(feeds):
            raise RuntimeError("all RSS feeds failed: " + "; ".join(errors[:5]))
        ctx.set_cursor(self.name, {"feeds": states, "failed": len(errors),
                                   "last_poll": utcnow().isoformat()})
        return batch
