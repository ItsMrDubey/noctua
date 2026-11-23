"""Public Telegram channels via their web preview (``https://t.me/s/<channel>``).

No login, bot or API key: the preview page is plain HTML listing the newest ~20 posts.
Channels come from ``config/sources.yaml`` (``telegram.channels: [{name, language,
country}]``). Requests are polite: at most ``concurrency`` in flight, starts at least
``min_interval_s`` apart, the shared client's User-Agent. Incremental per channel by
message id (cursor ``last_id``): only newer posts are emitted; when more than a page arrived
since the last poll, older pages (``?before=<id>``) are read up to ``max_pages``.

Posts become ``article`` Documents (metadata + link only: title = first line, snippet
<= 300 characters, canonical ``https://t.me/<channel>/<id>`` link) and go through the same
triage / clustering / translation as RSS items. All channels share the t.me domain, so
Telegram never counts as more than one outlet for multi-source status (channels repost each
other heavily). Media-only posts (no text) are skipped.

``base_url`` can point at the ``https://telegram.me`` mirror (same pages) where t.me is
blocked by DNS; links stay canonical t.me.
"""

import asyncio
import html
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from osint.backfill.common import Polite
from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.geo import utcnow
from osint.core.models import Batch, Document, clip_snippet, document_id

log = logging.getLogger(__name__)

BASE_URL = "https://t.me"
CANONICAL = "https://t.me"
MAX_PAGE_BYTES = 3 * 1024 * 1024
TITLE_MAX = 200
_CHANNEL = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_WRAP = re.compile(r'<div class="tgme_widget_message_wrap')
_POST = re.compile(r'data-post="([A-Za-z0-9_]+)/(\d+)"')
_TEXT = re.compile(r'<div class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>',
                   re.DOTALL)
_DATE = re.compile(r'class="tgme_widget_message_date"[^>]*>\s*<time datetime="([^"]+)"')
_VIEWS = re.compile(r'<span class="tgme_widget_message_views">([^<]+)</span>')
_FWD = re.compile(r'class="tgme_widget_message_forwarded_from_name"[^>]*>(.*?)</a>',
                  re.DOTALL)
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def html_text(fragment: str) -> str:
    """Message HTML -> plain text with line breaks kept (emoji images keep their char)."""
    text = html.unescape(_TAG.sub("", _BR.sub("\n", fragment)))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def parse_page(page: str, channel: str) -> list[dict[str, Any]]:
    """Posts of one preview page as {id, text, published, views, forwarded_from}, oldest
    first. Posts of other channels (none expected) and posts without text are skipped."""
    out: list[dict[str, Any]] = []
    for chunk in _WRAP.split(page)[1:]:
        post = _POST.search(chunk)
        if post is None or post.group(1).casefold() != channel.casefold():
            continue
        text_m = _TEXT.search(chunk)
        text = html_text(text_m.group(1)) if text_m else ""
        date_m = _DATE.search(chunk)
        if not text or date_m is None:
            continue
        published = datetime.fromisoformat(date_m.group(1)).astimezone(UTC)
        views = _VIEWS.search(chunk)
        fwd = _FWD.search(chunk)
        out.append({"id": int(post.group(2)), "text": text, "published": published,
                    "views": views.group(1).strip() if views else "",
                    "forwarded_from": html_text(fwd.group(1)) if fwd else ""})
    out.sort(key=lambda p: p["id"])
    return out


def has_preview(page: str) -> bool:
    return "tgme_channel_history" in page or "tgme_widget_message_wrap" in page


def page_ids(page: str, channel: str) -> list[int]:
    """Every post id on the page, text or not (media-only posts still advance paging)."""
    return sorted(int(i) for c, i in _POST.findall(page) if c.casefold() == channel.casefold())


def post_to_document(post: dict[str, Any], ch: dict[str, Any], fetched_at: datetime
                     ) -> Document:
    name = ch["name"]
    url = f"{CANONICAL}/{name}/{post['id']}"
    first = post["text"].split("\n", 1)[0]
    attrs: dict[str, object] = {"channel": name, "feed": f"telegram:{name}",
                                "country": ch.get("country") or "", "platform": "telegram"}
    if post.get("views"):
        attrs["views"] = post["views"]
    if post.get("forwarded_from"):
        attrs["forwarded_from"] = post["forwarded_from"]
    return Document(
        id=document_id(url), source=f"telegram:{name}", url=url,
        title=clip_snippet(first, TITLE_MAX), snippet=clip_snippet(post["text"]),
        language=str(ch.get("language") or ""), published_at=post["published"],
        fetched_at=fetched_at, kind="article", origin_id=f"{name}/{post['id']}", attrs=attrs)


@register
class TelegramCollector(PollCollector):
    name = "telegram"
    domain = "news"
    licence = ("Telegram public channel previews - metadata + links only; rights stay with "
               "channel owners")
    commercial_ok = False
    default_interval_s = 600.0
    poll_timeout_s = 600.0

    async def _get(self, ctx: CollectorContext, polite: Polite, url: str) -> str:
        async with polite:
            try:
                r = await ctx.http.get(url, timeout=30, headers={"Accept": "text/html"})
            except httpx.HTTPError as e:
                raise RuntimeError(type(e).__name__) from None
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        if len(r.content) > MAX_PAGE_BYTES:
            raise RuntimeError("page too large")
        return r.text

    async def _channel(self, ctx: CollectorContext, polite: Polite, ch: dict[str, Any],
                       state: dict[str, Any]) -> tuple[list[Document], dict[str, Any]]:
        name = ch["name"]
        base = str(self.cfg.get("base_url") or BASE_URL).rstrip("/")
        last = int(state.get("last_id") or 0)
        max_pages = max(1, int(self.cfg.get("max_pages", 3)))
        posts: dict[int, dict] = {}
        top = last
        url = f"{base}/s/{name}"
        for n in range(max_pages):
            page = await self._get(ctx, polite, url)
            if n == 0 and not has_preview(page):
                raise RuntimeError("no public web preview (private, restricted or renamed)")
            ids = page_ids(page, name)
            if not ids:
                break
            top = max(top, ids[-1])
            for p in await asyncio.to_thread(parse_page, page, name):
                if p["id"] > last:
                    posts[p["id"]] = p
            # first sight of a channel: one page only; otherwise page back until caught up
            if not last or ids[0] <= last + 1:
                break
            url = f"{base}/s/{name}?before={ids[0]}"
        fetched = utcnow()
        docs = [post_to_document(p, ch, fetched) for _, p in sorted(posts.items())]
        return docs, {"last_id": top, "last_post": fetched.isoformat() if docs
                      else state.get("last_post")}

    async def poll(self, ctx: CollectorContext) -> Batch:
        channels = []
        for ch in self.cfg.get("channels") or []:
            ch = {"name": ch} if isinstance(ch, str) else ch
            if isinstance(ch, dict) and _CHANNEL.match(str(ch.get("name") or "")):
                channels.append(ch)
            else:
                log.warning("telegram: ignoring invalid channel entry %r", ch)
        if not channels:
            raise RuntimeError("telegram: no channels configured")
        cursor = ctx.get_cursor(self.name) or {}
        states: dict[str, dict] = dict(cursor.get("channels") or {})
        polite = Polite(int(self.cfg.get("concurrency", 2)),
                        float(self.cfg.get("min_interval_s", 1.0)))
        results = await asyncio.gather(
            *(self._channel(ctx, polite, ch, states.get(ch["name"], {})) for ch in channels),
            return_exceptions=True)
        batch = Batch(source=self.name)
        errors: list[str] = []
        for ch, res in zip(channels, results, strict=True):
            if isinstance(res, BaseException):
                if isinstance(res, asyncio.CancelledError):
                    raise res
                errors.append(f"{ch['name']}: {type(res).__name__}: {res}"[:200])
                continue
            docs, states[ch["name"]] = res
            batch.documents.extend(docs)
        for err in errors:
            log.warning("telegram channel failed: %s", err)
        if len(errors) == len(channels):
            raise RuntimeError("all Telegram channels failed: " + "; ".join(errors[:5]))
        ctx.set_cursor(self.name, {"channels": states, "failed": len(errors),
                                   "last_poll": utcnow().isoformat()})
        return batch
