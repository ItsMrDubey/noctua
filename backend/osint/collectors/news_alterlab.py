"""AlterLab News API: free cached headlines (https://alterlab.io/news-api).

``GET https://alterlab.io/api/v1/news?category=&since=&limit=`` returns
``{"items": [{headline, url, source, source_name, published_at, author, image, snippet,
category, language}], "attribution": {"text": "News via AlterLab", ...}, ...}``.
Keyless use allows 60 requests/hour per IP; an optional free key (``X-API-Key``) allows
1,000/hour. Both are served from AlterLab's cache at no cost. One request per category per
poll (5 categories every 15 min = 20 req/h). The per-category cursor is the newest
``published_at`` seen, passed back as ``since``.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.geo import utcnow
from osint.core.models import Batch, Document, clip_snippet, document_id

log = logging.getLogger(__name__)

API_URL = "https://alterlab.io/api/v1/news"
ATTRIBUTION = "News via AlterLab"
DEFAULT_CATEGORIES = ["world", "general", "business", "technology", "science"]
DEFAULT_BACKFILL = timedelta(hours=6)


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_item(item: Any, fetched_at: datetime) -> Document | None:
    if not isinstance(item, dict):
        return None
    url, title = item.get("url"), item.get("headline") or item.get("title")
    published = _parse_ts(item.get("published_at"))
    if not isinstance(url, str) or not url.startswith(("http://", "https://")) \
            or not isinstance(title, str) or not title.strip() or published is None:
        return None
    return Document(
        id=document_id(url), source="news_alterlab", url=url, title=clip_snippet(title),
        snippet=clip_snippet(item.get("snippet") if isinstance(item.get("snippet"), str) else ""),
        language=str(item.get("language") or ""), published_at=published,
        fetched_at=fetched_at, kind="article", origin_id="",
        attrs={
            "attribution": ATTRIBUTION,
            "publisher": str(item.get("source_name") or item.get("source") or ""),
            "publisher_id": str(item.get("source") or ""),
            "category": str(item.get("category") or ""),
            "author": str(item.get("author") or ""),
        },
    )


@register
class AlterLabNewsCollector(PollCollector):
    name = "news_alterlab"
    domain = "news"
    licence = "AlterLab News API (attribution required)"
    commercial_ok = True
    default_interval_s = 900.0

    async def _fetch(self, ctx: CollectorContext, category: str, since: str | None,
                     key: str) -> list[Any]:
        params: dict[str, str | int] = {"category": category,
                                        "limit": int(self.cfg.get("limit", 100))}
        if since:
            params["since"] = since
        headers = {"Accept": "application/json"}
        if key:
            headers["X-API-Key"] = key
        try:
            r = await ctx.http.get(self.cfg.get("url", API_URL), params=params,
                                   headers=headers, timeout=30)
        except httpx.HTTPError as e:
            raise RuntimeError(f"{category}: {type(e).__name__}") from None
        if r.status_code >= 400:
            detail = r.text[:150].replace(key, "***") if key else r.text[:150]
            raise RuntimeError(f"{category}: HTTP {r.status_code} {detail.strip()}")
        try:
            body = r.json()
        except ValueError:
            raise RuntimeError(f"{category}: response is not JSON") from None
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise TypeError(f"{category}: response has no items list")
        return items

    async def poll(self, ctx: CollectorContext) -> Batch:
        key = ctx.settings.alterlab_api_key or ""
        categories = list(self.cfg.get("categories") or DEFAULT_CATEGORIES)
        cursor = ctx.get_cursor(self.name) or {}
        since_by_cat: dict[str, str] = dict(cursor.get("since") or {})
        default_since = (utcnow() - DEFAULT_BACKFILL).isoformat().replace("+00:00", "Z")
        results = await asyncio.gather(
            *(self._fetch(ctx, c, since_by_cat.get(c) or default_since, key)
              for c in categories),
            return_exceptions=True,
        )
        fetched = utcnow()
        batch = Batch(source=self.name)
        seen: set[str] = set()
        errors: list[str] = []
        skipped = 0
        for category, res in zip(categories, results, strict=True):
            if isinstance(res, BaseException):
                if isinstance(res, asyncio.CancelledError):
                    raise res
                msg = str(res)
                errors.append(msg.replace(key, "***") if key else msg)
                continue
            newest = _parse_ts(since_by_cat.get(category))
            for item in res:
                doc = parse_item(item, fetched)
                if doc is None:
                    skipped += 1
                    continue
                if newest is None or doc.published_at > newest:
                    newest = doc.published_at
                if doc.id not in seen:  # one article can be listed in several categories
                    seen.add(doc.id)
                    batch.documents.append(doc)
            if newest is not None:
                since_by_cat[category] = newest.isoformat().replace("+00:00", "Z")
        if skipped:
            log.warning("news_alterlab: skipped %d malformed item(s)", skipped)
        for err in errors:
            log.warning("news_alterlab category failed: %s", err)
        if errors and len(errors) == len(categories):
            raise RuntimeError("all AlterLab news categories failed: " + "; ".join(errors))
        ctx.set_cursor(self.name, {"since": since_by_cat})
        return batch
