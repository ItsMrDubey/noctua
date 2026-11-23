"""Third-party lookups for asset profiles, cached server-side and never awaited on the request
path.

``Lookups.peek(source, arg)`` answers from the SQLite cache (``data/profile_cache.sqlite``);
on a miss or an expired entry it schedules a background fetch and answers ``pending`` (or the
stale value). The UI polls the profile again while anything is pending. Per source: a minimum
interval between calls, a back-off after HTTP 429 (status ``rate_limited``), and separate TTLs
for hits and misses. Every request carries an identifying User-Agent with the
``OSINT_CONTACT`` address (.env), which planespotters.net and Wikimedia require.

Sources (all verified with curl on 2026-11-23):

* ``hexdb``        hexdb.io /api/v1/aircraft/{hex}: registration, manufacturer, type, registered
                   owner, operator flag code (PlaneBase / Jim Mason / Airport-Data data).
* ``adsbdb``       api.adsbdb.com /v0/aircraft/{hex}: same fields plus the owner's country.
* ``adsbdb_route`` api.adsbdb.com /v0/callsign/{cs}: airline and the route usually flown under
                   this callsign (David Taylor / Jim Mason route DB; display only).
* ``planespotters`` api.planespotters.net /pub/photos/hex|reg: one photo; terms: credit the
                   photographer next to the image, the image links to the photo page, image
                   URLs are used unchanged and loaded by the viewer's browser, JSON cached <= 24 h.
* ``commons``      Wikimedia Commons file search; kept only when the file title contains the
                   registration / IMO number, with author and licence from extmetadata.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

APP_UA = "Noctua-OSINT/0.1 (+{contact}; asset profiles, per-selection lookups)"
NO_CONTACT = "contact via OSINT_CONTACT not configured"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"


@dataclass(frozen=True)
class Spec:
    id: str
    name: str
    ttl_ok_s: float
    ttl_miss_s: float
    min_interval_s: float
    licence: str
    url: str
    attribution: str
    commercial: str  # ok | review | no (as in api/licences.py)
    notes: str = ""


DAY = 86400.0
SPECS: dict[str, Spec] = {s.id: s for s in [
    Spec("hexdb", "hexdb.io aircraft lookup", 7 * DAY, DAY, 1.0,
         "Free public API, no key; no licence stated", "https://hexdb.io/",
         "Aircraft data: hexdb.io (PlaneBase / PlanePlotter, Jim Mason, Steve Hibberd, "
         "Airport-Data)", "review",
         "Per-selection lookups only, cached 7 days. No terms published; commercial use "
         "would need permission."),
    Spec("adsbdb", "adsbdb.com aircraft lookup", 7 * DAY, DAY, 1.0,
         "Free public API; aircraft data from PlaneBase", "https://www.adsbdb.com/",
         "Aircraft data: adsbdb.com (PlaneBase)", "review",
         "Per-selection lookups only, cached 7 days."),
    Spec("adsbdb_route", "adsbdb.com callsign route", 6 * 3600.0, 6 * 3600.0, 1.0,
         "Route data (c) David J Taylor / Jim Mason: may not be copied or incorporated into "
         "other databases without permission", "https://www.adsbdb.com/",
         "Flight route: adsbdb.com (route data by David Taylor and Jim Mason)", "no",
         "Displayed per selection only; cached 6 h and never exported. The route is the one "
         "usually flown under the callsign, not a filed flight plan."),
    Spec("planespotters", "Planespotters.net photo API", 20 * 3600.0, 12 * 3600.0, 1.0,
         "Planespotters.net Photo API terms of use", "https://www.planespotters.net/photo/api",
         "Photo: photographer credit + link to the photo page on Planespotters.net", "review",
         "Credit shown next to the photo, which links to its Planespotters page (no nofollow); "
         "image URLs used unchanged and fetched by the viewer's browser; JSON cached < 24 h; "
         "never re-hosted, stored or used for ML."),
    Spec("commons", "Wikimedia Commons file search", 7 * DAY, 3 * DAY, 1.0,
         "Per-file licence (CC BY / CC BY-SA / public domain ...)",
         "https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia",
         "Photo: author and licence as listed on the Commons file page", "review",
         "Only files whose title contains the registration or IMO number are used; author, "
         "licence and a link to the file page are shown with the image."),
]}


class Result(dict):
    """{status, data, fetched_at, error?, stale?}; status: ok | not_found | rate_limited |
    error | pending | skipped."""


# ------------------------------------------------------------------ parsers (pure)


def _clean(v) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s or None


def parse_hexdb(j: dict) -> dict | None:
    if not isinstance(j, dict) or not (j.get("Registration") or j.get("ICAOTypeCode")):
        return None
    return {k: v for k, v in {
        "hex": _clean(j.get("ModeS")), "reg": _clean(j.get("Registration")),
        "manufacturer": _clean(j.get("Manufacturer")), "icao_type": _clean(j.get("ICAOTypeCode")),
        "type": _clean(j.get("Type")), "owner": _clean(j.get("RegisteredOwners")),
        "operator_flag": _clean(j.get("OperatorFlagCode")),
    }.items() if v}


def parse_adsbdb_aircraft(j: dict) -> dict | None:
    a = ((j or {}).get("response") or {})
    a = a.get("aircraft") if isinstance(a, dict) else None
    if not isinstance(a, dict):
        return None
    return {k: v for k, v in {
        "hex": _clean(a.get("mode_s")), "reg": _clean(a.get("registration")),
        "manufacturer": _clean(a.get("manufacturer")), "icao_type": _clean(a.get("icao_type")),
        "type": _clean(a.get("type")), "owner": _clean(a.get("registered_owner")),
        "owner_country": _clean(a.get("registered_owner_country_name")),
        "owner_country_iso": _clean(a.get("registered_owner_country_iso_name")),
        "operator_flag": _clean(a.get("registered_owner_operator_flag_code")),
    }.items() if v}


def _airport(a: dict | None) -> dict | None:
    if not isinstance(a, dict):
        return None
    return {k: a.get(k) for k in ("icao_code", "iata_code", "name", "municipality",
                                  "country_name", "country_iso_name", "latitude", "longitude")}


def parse_adsbdb_route(j: dict) -> dict | None:
    r = ((j or {}).get("response") or {})
    r = r.get("flightroute") if isinstance(r, dict) else None
    if not isinstance(r, dict):
        return None
    al = r.get("airline") or {}
    return {"callsign": r.get("callsign"), "callsign_iata": r.get("callsign_iata"),
            "airline": {k: al.get(k) for k in ("name", "icao", "iata", "country", "callsign")}
            if al else None,
            "origin": _airport(r.get("origin")), "destination": _airport(r.get("destination"))}


def parse_planespotters(j: dict) -> dict | None:
    photos = (j or {}).get("photos") or []
    if not photos:
        return None
    p = photos[0]
    big = (p.get("thumbnail_large") or {}).get("src")
    small = (p.get("thumbnail") or {}).get("src")
    if not (big or small) or not p.get("link"):
        return None
    # URLs are kept exactly as returned (terms: no rewriting / proxying)
    return {"id": p.get("id"), "image": big or small, "thumb": small or big,
            "link": p["link"], "credit": _clean(p.get("photographer")) or "unknown photographer",
            "size": (p.get("thumbnail_large") or p.get("thumbnail") or {}).get("size")}


_TAG = re.compile(r"<[^>]+>")


def _strip(v: str | None) -> str | None:
    return _clean(html.unescape(_TAG.sub("", v or "")))


def _norm_token(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def parse_commons(j: dict, token: str) -> dict | None:
    """First search hit whose title contains ``token`` (letters / digits only)."""
    want = _norm_token(token)
    pages = sorted(((j or {}).get("query") or {}).get("pages", {}).values(),
                   key=lambda p: p.get("index", 0))
    for p in pages:
        title = p.get("title") or ""
        if want not in _norm_token(title):
            continue
        ii = (p.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata") or {}
        img = ii.get("thumburl") or ii.get("url")
        if not img:
            continue
        def val(k: str, meta: dict = meta) -> str | None:
            return _strip((meta.get(k) or {}).get("value"))
        return {"title": title.removeprefix("File:"), "image": img,
                "link": ii.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/{title}",
                "credit": val("Artist") or "unknown author", "licence": val("LicenseShortName"),
                "licence_url": val("LicenseUrl"), "date": val("DateTimeOriginal"),
                "description": (val("ImageDescription") or "")[:200] or None}
    return None


# ------------------------------------------------------------------ cache


class Cache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, status TEXT "
                         "NOT NULL, data TEXT, error TEXT, fetched_at REAL NOT NULL, "
                         "expires_at REAL NOT NULL)")
        self._lock = threading.Lock()

    def get(self, key: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT status, data, error, fetched_at, expires_at FROM cache "
                                 "WHERE key=?", (key,)).fetchone()
        if r is None:
            return None
        return {"status": r[0], "data": json.loads(r[1]) if r[1] else None, "error": r[2],
                "fetched_at": r[3], "expires_at": r[4]}

    def put(self, key: str, status: str, data, error: str | None, ttl_s: float) -> None:
        now = time.time()
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?,?)",
                             (key, status, json.dumps(data) if data is not None else None,
                              error, now, now + ttl_s))

    def prune(self, older_than_s: float = 30 * DAY) -> None:
        with self._lock:
            self._db.execute("DELETE FROM cache WHERE expires_at < ?", (time.time() - older_than_s,))

    def close(self) -> None:
        self._db.close()


# ------------------------------------------------------------------ fetchers


class RateLimited(Exception):
    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"rate limited for {retry_after_s:.0f} s")
        self.retry_after_s = retry_after_s


def _retry_after(r: httpx.Response) -> float:
    try:
        return max(30.0, float(r.headers.get("retry-after") or 300))
    except ValueError:
        return 300.0


async def _get_json(http: httpx.AsyncClient, url: str, **kw) -> tuple[int, dict | None]:
    r = await http.get(url, timeout=12.0, **kw)
    if r.status_code == 429:
        raise RateLimited(_retry_after(r))
    if r.status_code == 404:
        return 404, None
    if r.status_code >= 400:
        raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
    try:
        return r.status_code, r.json()
    except ValueError as exc:
        raise httpx.DecodingError(f"bad JSON from {url}") from exc


async def f_hexdb(http, arg: str):
    _, j = await _get_json(http, f"https://hexdb.io/api/v1/aircraft/{arg}")
    return parse_hexdb(j) if j else None


async def f_adsbdb(http, arg: str):
    _, j = await _get_json(http, f"https://api.adsbdb.com/v0/aircraft/{arg}")
    return parse_adsbdb_aircraft(j) if j else None


async def f_adsbdb_route(http, arg: str):
    _, j = await _get_json(http, f"https://api.adsbdb.com/v0/callsign/{arg}")
    return parse_adsbdb_route(j) if j else None


async def f_planespotters(http, arg: str):
    hex_code, _, reg = arg.partition("|")
    for kind, val in (("hex", hex_code), ("reg", reg)):
        if not val:
            continue
        _, j = await _get_json(http, f"https://api.planespotters.net/pub/photos/{kind}/{val}")
        got = parse_planespotters(j) if j else None
        if got:
            return {**got, "matched_on": kind}
    return None


async def f_commons(http, arg: str):
    params = {"action": "query", "format": "json", "generator": "search",
              "gsrsearch": f'intitle:"{arg}" filetype:bitmap', "gsrnamespace": "6",
              "gsrlimit": "6", "prop": "imageinfo", "iiprop": "url|extmetadata",
              "iiurlwidth": "640",
              "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl|DateTimeOriginal|"
                                     "ImageDescription"}
    _, j = await _get_json(http, COMMONS_API, params=params)
    return parse_commons(j, arg) if j else None


FETCHERS: dict[str, Callable[[httpx.AsyncClient, str], Awaitable[dict | None]]] = {
    "hexdb": f_hexdb, "adsbdb": f_adsbdb, "adsbdb_route": f_adsbdb_route,
    "planespotters": f_planespotters, "commons": f_commons,
}


# ------------------------------------------------------------------ orchestration


class Lookups:
    def __init__(self, cache_path: Path, contact: str | None = None,
                 client: httpx.AsyncClient | None = None,
                 fetchers: dict | None = None) -> None:
        self.cache = Cache(cache_path)
        self.ua = APP_UA.format(contact=contact or NO_CONTACT)
        self._client = client
        self.fetchers = fetchers or FETCHERS
        self._inflight: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self._blocked_until: dict[str, float] = {}

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(headers={"User-Agent": self.ua},
                                             follow_redirects=True)
        return self._client

    @staticmethod
    def key(source: str, arg: str) -> str:
        return f"{source}:{arg}"

    def peek(self, source: str, arg: str | None) -> Result:
        """Cached result or ``pending`` (a background fetch is then running)."""
        if not arg:
            return Result(status="skipped")
        key = self.key(source, arg)
        hit = self.cache.get(key)
        now = time.time()
        if hit is not None and hit["expires_at"] > now:
            return Result(status=hit["status"], data=hit["data"], fetched_at=hit["fetched_at"],
                          error=hit["error"])
        self.schedule(source, arg)
        if hit is not None and hit["status"] in ("ok", "not_found"):  # stale but usable
            return Result(status=hit["status"], data=hit["data"], fetched_at=hit["fetched_at"],
                          stale=True)
        if self._blocked_until.get(source, 0) > now:
            return Result(status="rate_limited", retry_at=self._blocked_until[source])
        return Result(status="pending")

    def schedule(self, source: str, arg: str) -> None:
        key = self.key(source, arg)
        if key in self._inflight:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync caller): nothing to schedule, stays pending
        task = loop.create_task(self._run(source, arg))
        self._inflight[key] = task
        task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))

    async def fetch_now(self, source: str, arg: str) -> Result:
        """Fetch (respecting pacing) and cache; used by the background task and tests."""
        await self._run(source, arg)
        return self.peek(source, arg)

    async def _run(self, source: str, arg: str) -> None:
        spec = SPECS[source]
        lock = self._locks.setdefault(source, asyncio.Lock())
        key = self.key(source, arg)
        async with lock:  # one request at a time per source, min interval between calls
            if self._blocked_until.get(source, 0) > time.time():
                return
            wait = self._last.get(source, 0) + spec.min_interval_s - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[source] = time.monotonic()
            try:
                data = await self.fetchers[source](self.http, arg)
            except RateLimited as exc:
                self._blocked_until[source] = time.time() + exc.retry_after_s
                self.cache.put(key, "rate_limited", None, str(exc), min(exc.retry_after_s, 900))
                log.warning("profile lookup %s rate limited (%s)", source, exc)
                return
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                self.cache.put(key, "error", None, f"{type(exc).__name__}: {exc}"[:300], 600)
                log.info("profile lookup %s %s failed: %s", source, arg, exc)
                return
            if data:
                self.cache.put(key, "ok", data, None, spec.ttl_ok_s)
            else:
                self.cache.put(key, "not_found", None, None, spec.ttl_miss_s)

    async def aclose(self) -> None:
        for t in list(self._inflight.values()):
            t.cancel()
        if self._client is not None:
            await self._client.aclose()
        self.cache.close()
