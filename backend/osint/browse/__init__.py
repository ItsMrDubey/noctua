"""In-app browser backend: reader view for arbitrary pages.

``read(url)`` fetches a page server-side and returns a reader view (title, site, image,
paragraphs) plus whether the site allows embedding (X-Frame-Options / CSP frame-ancestors),
so the UI can choose live view or reader view. Requests to private / loopback addresses are
refused (checked on every redirect hop). Pages are cached in memory for 30 minutes.

Wikidata / Wikipedia / Commons are queried by the frontend directly (CORS), as ordinary
user browsing: Wikimedia's robot policy wants contact details from scripted clients.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from collections import OrderedDict
from html import unescape
from urllib.parse import urljoin, urlsplit

import httpx

from osint.pipeline.extract.config import FetchConfig
from osint.pipeline.extract.fetch import FetchFailed, fetch_alterlab
from osint.pipeline.extract.readability import extract_article

MAX_PAGE_BYTES = 3_000_000



class _TTL:
    def __init__(self, ttl: float, size: int = 500) -> None:
        self.ttl, self.size, self.d = ttl, size, OrderedDict()

    def get(self, k):
        v = self.d.get(k)
        if v is None or time.monotonic() - v[0] > self.ttl:
            return None
        self.d.move_to_end(k)
        return v[1]

    def put(self, k, v) -> None:
        self.d[k] = (time.monotonic(), v)
        self.d.move_to_end(k)
        while len(self.d) > self.size:
            self.d.popitem(last=False)


class BlockedURL(ValueError):
    pass




def _host_is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            return False
    return bool(infos)


def check_url(url: str, resolve=_host_is_public) -> str:
    """Only public http(s) URLs (no credentials, no private / loopback hosts)."""
    p = urlsplit(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise BlockedURL("only public http(s) URLs")
    host = p.hostname
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise BlockedURL("local host")
    if not resolve(host):
        raise BlockedURL("host resolves to a private or unknown address")
    return url.strip()


def frameable(headers: httpx.Headers) -> bool:
    xfo = (headers.get("x-frame-options") or "").lower()
    if "deny" in xfo or "sameorigin" in xfo or "allow-from" in xfo:
        return False
    csp = (headers.get("content-security-policy") or "").lower()
    m = re.search(r"frame-ancestors([^;]*)", csp)
    if m:
        srcs = m.group(1).split()
        return "*" in srcs
    return True


_META_RE = re.compile(r"<meta\s+[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([a-zA-Z:_-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')')


def page_meta(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tag in _META_RE.findall(html[:200_000]):
        attrs = {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4) or "")
                 for m in _ATTR_RE.finditer(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key and "content" in attrs and key not in out:
            out[key] = unescape(attrs["content"]).strip()
    return out








def _paragraphs(art) -> list[str]:
    if art is None:
        return []
    paras = list(dict.fromkeys(art.paragraphs))
    if sum(map(len, paras)) < 200 and art.body_chars > 200:
        # body came from JSON-LD articleBody: split the text into readable chunks
        body = art.text.split(chr(10))
        paras = [b for b in body if len(b) > 30]
    return paras[:120]


class Browser:
    def __init__(self, client: httpx.AsyncClient | None = None, resolve_host=_host_is_public,
                 alterlab_key: str = "") -> None:
        self._client = client
        self.resolve_host = resolve_host
        self.alterlab_key = alterlab_key
        self.c_read = _TTL(1800, 200)

    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0, follow_redirects=False)
        return self._client

    # ---- reader ----
    async def read(self, url: str) -> dict:
        url = await asyncio.to_thread(check_url, url, self.resolve_host)
        hit = self.c_read.get(url)
        if hit is not None:
            return hit
        cur = url
        for _ in range(6):
            req = self.http().build_request("GET", cur, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en;q=0.9,*;q=0.5"})
            r = await self.http().send(req, stream=True, follow_redirects=False)
            try:
                if r.is_redirect and r.headers.get("location"):
                    cur = await asyncio.to_thread(check_url, urljoin(cur, r.headers["location"]), self.resolve_host)
                    continue
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES:
                        break
                status, headers = r.status_code, r.headers
                ctype = headers.get("content-type", "")
            finally:
                await r.aclose()
            break
        else:
            raise BlockedURL("too many redirects")
        html = bytes(body).decode(r.encoding or "utf-8", errors="replace") if "html" in ctype or not ctype else ""
        art = extract_article(html, max_chars=40_000, min_par=30) if html else None
        meta = page_meta(html) if html else {}
        img = meta.get("og:image") or meta.get("twitter:image") or ""
        img = urljoin(cur, img) if img else ""
        site = meta.get("og:site_name") or (urlsplit(cur).hostname or "").removeprefix("www.")
        out = {
            "url": url, "final_url": cur, "status": status,
            "frameable": frameable(headers) and status < 400 and cur.startswith("https://"),
            "title": (art.title if art else "") or meta.get("og:title") or cur,
            "description": (art.description if art else "") or "",
            "site": site, "image": img if img.startswith(("http://", "https://")) else "",
            "published": meta.get("article:published_time") or meta.get("og:updated_time") or "",
            "author": meta.get("author") or meta.get("article:author") or "",
            "paragraphs": _paragraphs(art),
            "content_type": ctype.split(";")[0],
        }
        out["via"] = "direct"
        if len(out["paragraphs"]) < 3 and self.alterlab_key:
            # bot wall / JS-only page: one AlterLab scrape (bounded credits), user-initiated
            try:
                text, _credits = await fetch_alterlab(self.http(), cur, self.alterlab_key,
                                                      FetchConfig(min_chars=200))
                paras = [ln for ln in text.split(chr(10)) if len(ln) > 30]
                if len(paras) >= 3:
                    out["paragraphs"], out["via"] = paras[:120], "alterlab"
                    if out["title"] in (cur, "Just a moment...") and paras:
                        out["title"] = text.split(chr(10))[0][:200]
            except FetchFailed:
                pass
        self.c_read.put(url, out)
        return out
