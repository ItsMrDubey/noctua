"""Fetch an event's primary article text (kept in memory only; never persisted).

Order: plain GET + readability extractor -> AlterLab scrape (if a key is set) -> the
title + snippet we already have (``text_source='snippet'``).
"""

import logging
import re
from dataclasses import dataclass

import httpx

from osint.pipeline.extract.config import FetchConfig
from osint.pipeline.extract.readability import collapse, extract_article

log = logging.getLogger(__name__)
USER_AGENT = "osint-poc/0.1 (article extraction)"
ALTERLAB_SCRAPE_URL = "https://api.alterlab.io/api/v1/scrape"
_CHARSET_RE = re.compile(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_-]+)", re.IGNORECASE)
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


@dataclass
class ArticleText:
    text: str
    source: str  # direct | alterlab | snippet
    error: str | None = None
    credits: float = 0.0


class FetchFailed(Exception):
    pass


def _decode(raw: bytes, header_charset: str | None) -> str:
    enc = header_charset
    if not enc:
        m = _CHARSET_RE.search(raw[:4096])
        enc = m.group(1).decode("ascii") if m else "utf-8"
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


async def fetch_direct(client: httpx.AsyncClient, url: str, cfg: FetchConfig) -> str:
    """Main text of ``url`` via plain GET; raises FetchFailed when unusable."""
    try:
        async with client.stream("GET", url, timeout=cfg.timeout_s,
                                  headers={"User-Agent": USER_AGENT,
                                           "Accept": "text/html,application/xhtml+xml"}) as r:
            if r.status_code >= 400:
                raise FetchFailed(f"HTTP {r.status_code}")
            ctype = r.headers.get("content-type", "")
            if ctype and "html" not in ctype and "xml" not in ctype:
                raise FetchFailed(f"not html ({ctype[:40]})")
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) >= cfg.max_bytes:
                    break
            charset = r.charset_encoding
    except httpx.HTTPError as exc:
        raise FetchFailed(f"{type(exc).__name__}: {exc}"[:200]) from None
    art = extract_article(_decode(bytes(buf[:cfg.max_bytes]), charset),
                          max_chars=cfg.max_chars)
    if art.body_chars < cfg.min_chars:
        raise FetchFailed(f"too little text ({art.body_chars} chars; paywall/JS?)")
    return art.text


def _markdown_to_text(md: str) -> str:
    md = _MD_IMG_RE.sub("", md)
    md = _MD_LINK_RE.sub(r"\1", md)
    lines = [collapse(line.lstrip("#>*-| ")) for line in md.splitlines()]
    return "\n".join(line for line in lines if len(line) >= 3)


def _alterlab_text(body: dict) -> str:
    content = body.get("content")
    for cand in (content.get("markdown") if isinstance(content, dict) else None,
                 body.get("markdown"),
                 content.get("text") if isinstance(content, dict) else None,
                 body.get("filtered_content"),
                 content if isinstance(content, str) else None):
        if isinstance(cand, str) and cand.strip():
            if cand.lstrip()[:1] == "<":
                return extract_article(cand).text
            return _markdown_to_text(cand)
    return ""


async def fetch_alterlab(client: httpx.AsyncClient, url: str, key: str,
                         cfg: FetchConfig) -> tuple[str, float]:
    try:
        r = await client.post(
            ALTERLAB_SCRAPE_URL, headers={"X-API-Key": key},
            json={"url": url, "formats": ["markdown"],
                  "cost_controls": {"max_credits": cfg.alterlab_max_credits}},
            timeout=90.0)
    except httpx.HTTPError as exc:
        raise FetchFailed(f"alterlab {type(exc).__name__}") from None
    if r.status_code != 200:  # 202 = async job handoff: not worth polling for this
        raise FetchFailed(f"alterlab HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        raise FetchFailed("alterlab: bad JSON") from None
    text = _alterlab_text(body if isinstance(body, dict) else {})[:cfg.max_chars]
    billing = body.get("billing") if isinstance(body, dict) else None
    credits = billing.get("total_credits") if isinstance(billing, dict) else 0
    if len(text) < cfg.min_chars:
        raise FetchFailed("alterlab: too little text")
    return text, float(credits or 0)


async def get_article_text(client: httpx.AsyncClient, url: str, *, title: str, snippet: str,
                           cfg: FetchConfig, alterlab_key: str = "") -> ArticleText:
    errors: list[str] = []
    if url.startswith(("http://", "https://")):
        try:
            return ArticleText(await fetch_direct(client, url, cfg), "direct")
        except FetchFailed as exc:
            errors.append(f"direct: {exc}")
        if alterlab_key and cfg.alterlab_fallback:
            try:
                text, credits = await fetch_alterlab(client, url, alterlab_key, cfg)
                return ArticleText(text, "alterlab", "; ".join(errors), credits)
            except FetchFailed as exc:
                errors.append(str(exc))
    else:
        errors.append("no http url")
    text = "\n".join(x for x in (title, snippet) if x)
    return ArticleText(text, "snippet", "; ".join(errors) or None)
