"""Impact sites and affected areas: a grounded geocoding cascade for named strike /
disaster sites, plus honest affected-area geometries.

Cascade per extracted site (each step labels its precision and confidence):

a. coordinates written in the article             -> ``exact``
b. local reference POIs (OurAirports, ports, curated military bases, nuclear sites),
   constrained to the event's surroundings          -> ``facility``
c. OpenStreetMap Nominatim search, bounded to a viewbox around the event's city
   (1 request/s process-wide, every answer cached forever, daily cap) -> ``facility`` /
   ``neighbourhood`` / ``settlement``
d. the city gazetteer / the event's own location    -> ``city`` (or coarser)

A wrong precise point is worse than an honest city point: every step needs the site's
name to match the candidate, and candidates must lie near the event's known location.

Affected areas (``areas``), each with a ``basis``:
* circles around located sites whose radius reflects location uncertainty + typical effect
* earthquakes: USGS ShakeMap MMI contours (MMI >= V) when published, else a magnitude
  felt-radius estimate
* fires: the FIRMS hotspot hull near the event
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import math
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from osint.core.geo import haversine_km
from osint.enrich.gazetteer import fold

log = logging.getLogger(__name__)

IMPACT_VERSION = 1  # bump when cascade/area semantics change (records are recomputed)
USER_AGENT = "osint-poc/0.1 (local research prototype)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USGS_DETAIL_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/detail/{id}.geojson"

# precision labels, finest first
PRECISION_RANK = {"exact": 5, "facility": 4, "neighbourhood": 3, "settlement": 3,
                  "city": 2, "admin1": 1, "country": 0}
FINE = {"exact", "facility", "neighbourhood", "settlement"}
# radius (km) of the "uncertainty radius" area per precision; facilities by kind
AREA_KM = {"exact": 0.5, "neighbourhood": 2.0, "settlement": 1.5, "city": 8.0}
FACILITY_KM = {"airport": 2.0, "airbase": 2.0, "port": 1.5, "naval_base": 1.5,
               "military_base": 1.0, "refinery": 1.0, "oil_depot": 0.8, "power_plant": 1.0,
               "factory": 0.8, "substation": 0.5, "bridge": 0.5, "rail": 0.8,
               "residential": 0.5, "government": 0.5, "hospital": 0.5, "school": 0.5,
               "market": 0.5}
# how far from the anchor (event / context city) a candidate may lie, by anchor precision
ANCHOR_KM = {"exact": 60.0, "point": 60.0, "city": 60.0, "admin1": 250.0}
VIEWBOX_DEG = {"point": 0.4, "city": 0.4, "admin1": 1.6}

# words that never identify a facility on their own
GENERIC = frozenset({
    "airport", "airports", "international", "intl", "air", "airbase", "airfield",
    "aerodrome", "airstrip", "field", "base", "bases", "port", "seaport", "harbour",
    "harbor", "naval", "navy", "military", "army", "station", "the", "of", "de", "del",
    "la", "le", "el", "al", "oil", "refinery", "depot", "terminal", "plant", "power",
    "nuclear", "npp", "thermal", "hydroelectric", "city", "regional", "municipal",
    "domestic", "facility", "complex", "site", "area", "zone", "district", "region",
    "province", "oblast", "county", "village", "town", "near", "central", "main", "new",
    "old", "and", "in", "at", "on", "a", "an", "fuel", "storage", "tank", "farm",
    "depots"
})
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(fold(text or ""))


def distinctive(text: str) -> set[str]:
    return {t for t in tokens(text) if t not in GENERIC and len(t) > 1}


def name_match(site_name: str, candidate: str) -> float:
    """0..1: how well ``candidate`` carries the distinctive words of ``site_name``
    (0 when the site has none: a bare 'airport' never matches by name)."""
    want = distinctive(site_name)
    if not want:
        return 0.0
    have = set(tokens(candidate))
    hit = len(want & have) / len(want)
    if hit >= 1.0:
        return 1.0
    a, b = " ".join(sorted(want)), " ".join(sorted(distinctive(candidate)))
    ratio = difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0
    return max(hit if hit >= 0.5 else 0.0, ratio if ratio >= 0.85 else 0.0)


# ------------------------------------------------------------------------- geometry


def circle(lat: float, lon: float, radius_km: float, n: int = 48) -> dict:
    """GeoJSON Polygon approximating a circle (small-radius equirectangular)."""
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(0.05, math.cos(math.radians(lat))))
    ring = [[round(lon + dlon * math.cos(2 * math.pi * i / n), 5),
             round(lat + dlat * math.sin(2 * math.pi * i / n), 5)] for i in range(n)]
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Monotone-chain hull of (lon, lat) points (counter-clockwise, not closed)."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def buffered_hull(points: list[tuple[float, float]], buffer_km: float) -> dict:
    """Polygon around (lon, lat) points: the hull of each point's ``buffer_km`` circle
    (a cheap Minkowski-sum approximation; covers the detection footprints)."""
    ring_pts: list[tuple[float, float]] = []
    for lon, lat in points:
        ring_pts += [tuple(c) for c in circle(lat, lon, buffer_km, n=12)["coordinates"][0]]
    hull = convex_hull(ring_pts)
    ring = [[round(x, 5), round(y, 5)] for x, y in hull]
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def geometry_bounds(geom: dict) -> tuple[float, float, float, float] | None:
    """(west, south, east, north) of a GeoJSON geometry."""
    xs: list[float] = []
    ys: list[float] = []

    def walk(c):
        if c and isinstance(c[0], int | float):
            xs.append(float(c[0]))
            ys.append(float(c[1]))
        else:
            for x in c:
                walk(x)

    walk((geom or {}).get("coordinates") or [])
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def union_bounds(boxes) -> tuple[float, float, float, float] | None:
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def felt_radius_km(mag: float) -> float:
    """Rough radius of MMI >= V shaking from magnitude alone (labelled an estimate)."""
    return round(min(600.0, max(2.0, 10 ** (0.5 * mag - 1.0))), 1)


# ------------------------------------------------------------------------- cache


class HttpCache:
    """Every external lookup answer (positive and negative) in SQLite; ``ttl_s`` None
    = forever. Its own file (data_dir/geocode_cache.sqlite), worker-only."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS http_cache (provider TEXT NOT NULL, key TEXT NOT "
            "NULL, payload TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL, "
            "PRIMARY KEY (provider, key))")

    def get(self, provider: str, key: str, now: float | None = None):
        """(True, payload) on a live hit, (False, None) otherwise."""
        now = time.time() if now is None else now
        with self._lock:
            row = self._db.execute(
                "SELECT payload, expires_at FROM http_cache WHERE provider=? AND key=?",
                (provider, key)).fetchone()
        if row is None or (row[1] is not None and row[1] < now):
            return False, None
        return True, json.loads(row[0])

    def put(self, provider: str, key: str, payload, ttl_s: float | None = None,
            now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?,?)",
                (provider, key, json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                 now, None if ttl_s is None else now + ttl_s))

    def count(self, provider: str, since: float) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM http_cache WHERE provider=? AND created_at >= ?",
                (provider, since)).fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()


# ------------------------------------------------------------------------- Nominatim

# process-wide request gate: Nominatim's usage policy allows at most 1 request/second
_GATE = threading.Lock()
_next_slot = [0.0]


async def _wait_turn(min_interval: float, clock=time.monotonic) -> None:
    with _GATE:
        now = clock()
        slot = max(now, _next_slot[0])
        _next_slot[0] = slot + min_interval
    if slot > now:
        await asyncio.sleep(slot - now)


class Nominatim:
    """OSM Nominatim search with the usage policy built in: <= 1 request/s process-wide,
    a descriptive generic User-Agent (never personal data), every answer cached forever
    (negative too), a daily request cap and a cool-down after 403/429/5xx."""

    provider = "nominatim"

    def __init__(self, http: httpx.AsyncClient, cache: HttpCache, *, enabled: bool = True,
                 min_interval_s: float = 1.1, daily_cap: int = 500,
                 cooldown_s: float = 3600.0, url: str = NOMINATIM_URL,
                 clock: Callable[[], float] = time.time) -> None:
        self.http, self.cache, self.enabled = http, cache, enabled
        self.min_interval_s, self.daily_cap, self.cooldown_s = (
            max(1.0, min_interval_s), daily_cap, cooldown_s)
        self.url, self.clock = url, clock
        self._blocked_until = 0.0
        self.requests = 0
        self.cache_hits = 0
        self.last_error: str | None = None

    @staticmethod
    def key(params: dict) -> str:
        return json.dumps(sorted(params.items()), ensure_ascii=False)

    async def search(self, query: str, *, countrycode: str | None = None,
                     viewbox: tuple[float, float, float, float] | None = None
                     ) -> list[dict] | None:
        """Results (possibly []) or None when no answer is available (disabled, capped,
        cooling down, network error: nothing is cached then)."""
        params = {"q": query.strip()[:200], "format": "jsonv2", "limit": "5",
                  "accept-language": "en"}
        if countrycode:
            params["countrycodes"] = countrycode.lower()
        if viewbox:
            w, s, e, n = viewbox
            params["viewbox"] = f"{w:.4f},{n:.4f},{e:.4f},{s:.4f}"
            params["bounded"] = "1"
        key = self.key(params)
        hit, payload = self.cache.get(self.provider, key)
        if hit:
            self.cache_hits += 1
            return payload
        now = self.clock()
        if not self.enabled or now < self._blocked_until:
            return None
        if self.cache.count(self.provider, now - 86400) >= self.daily_cap:
            return None
        await _wait_turn(self.min_interval_s)
        self.requests += 1
        try:
            r = await self.http.get(self.url, params=params,
                                    headers={"User-Agent": USER_AGENT}, timeout=20.0)
        except httpx.HTTPError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            return None
        if r.status_code in (403, 429) or r.status_code >= 500:
            self._blocked_until = self.clock() + self.cooldown_s
            self.last_error = f"HTTP {r.status_code}"
            return None
        if r.status_code != 200:
            self.last_error = f"HTTP {r.status_code}"
            return None
        try:
            data = r.json()
        except ValueError:
            self.last_error = "invalid JSON"
            return None
        results = [_slim_nominatim(x) for x in data if isinstance(x, dict)] \
            if isinstance(data, list) else []
        self.cache.put(self.provider, key, results)
        return results


def _slim_nominatim(x: dict) -> dict:
    return {k: x.get(k) for k in ("lat", "lon", "name", "display_name", "category", "type",
                                  "addresstype", "importance", "boundingbox", "osm_type",
                                  "osm_id")}


_SETTLEMENT = {"village", "hamlet", "isolated_dwelling", "farm", "locality"}
_NEIGHBOURHOOD = {"suburb", "neighbourhood", "quarter", "borough", "city_district",
                  "city_block", "residential", "allotments", "district"}
_CITY = {"city", "town", "municipality", "county", "state", "region", "province",
         "administrative", "country"}


def nominatim_precision(r: dict) -> str:
    """Precision label for a Nominatim result from its class/type and extent."""
    cat, typ = r.get("category") or "", r.get("type") or ""
    addr = r.get("addresstype") or ""
    if cat == "boundary" or typ in _CITY or addr in _CITY:
        return "city"
    if typ in _SETTLEMENT or addr in _SETTLEMENT:
        label = "settlement"
    elif typ in _NEIGHBOURHOOD or addr in _NEIGHBOURHOOD or cat == "place":
        label = "neighbourhood"
    else:
        label = "facility"
    try:
        s, n, w, e = (float(v) for v in r.get("boundingbox") or [])
        lat = (s + n) / 2
        diag = haversine_km(s, w, n, e) if abs(lat) < 89 else 0.0
    except (TypeError, ValueError):
        diag = 0.0
    if diag > 30:
        return "city"
    if label == "facility" and diag > 12:
        return "neighbourhood"
    return label


# ------------------------------------------------------------------------- USGS


class Usgs:
    """USGS event detail + ShakeMap MMI contours (cached; a missing ShakeMap is cached
    only briefly because it is often published hours after the event)."""

    provider = "usgs"

    def __init__(self, http: httpx.AsyncClient, cache: HttpCache, *, enabled: bool = True,
                 negative_ttl_s: float = 6 * 3600) -> None:
        self.http, self.cache, self.enabled = http, cache, enabled
        self.negative_ttl_s = negative_ttl_s
        self.last_error: str | None = None

    async def _get_json(self, url: str):
        try:
            r = await self.http.get(url, headers={"User-Agent": USER_AGENT}, timeout=30.0)
        except httpx.HTTPError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            return None
        if r.status_code != 200:
            self.last_error = f"HTTP {r.status_code}"
            return None
        try:
            return r.json()
        except ValueError:
            return None

    async def shakemap(self, usgs_id: str) -> dict | None:
        """{"mag", "mmi_contours": [{"value", "geometry"}]} or None (no data yet)."""
        if not re.fullmatch(r"[a-z0-9]{2,20}", usgs_id or ""):
            return None
        hit, payload = self.cache.get(self.provider, usgs_id)
        if hit:
            return payload or None
        if not self.enabled:
            return None
        detail = await self._get_json(USGS_DETAIL_URL.format(id=usgs_id))
        if detail is None:
            return None  # network error: not cached
        props = detail.get("properties") or {}
        out = {"mag": props.get("mag"), "mmi_contours": []}
        url = None
        for prod in (props.get("products") or {}).get("shakemap") or []:
            content = (prod.get("contents") or {}).get("download/cont_mmi.json") or {}
            if content.get("url"):
                url = content["url"]
                break
        if url and url.startswith("https://earthquake.usgs.gov/"):
            cont = await self._get_json(url)
            for f in (cont or {}).get("features") or []:
                val = (f.get("properties") or {}).get("value")
                geom = f.get("geometry") or {}
                if isinstance(val, int | float) and geom.get("type") in (
                        "LineString", "MultiLineString", "Polygon", "MultiPolygon"):
                    out["mmi_contours"].append({"value": float(val), "geometry": geom})
        if out["mmi_contours"]:
            self.cache.put(self.provider, usgs_id, out)
        else:
            self.cache.put(self.provider, usgs_id, out, ttl_s=self.negative_ttl_s)
        return out


def mmi_area(contours: list[dict], min_mmi: float = 5.0) -> dict | None:
    """MultiPolygon from the lowest ShakeMap contour level >= ``min_mmi`` (contour lines
    are closed into rings)."""
    levels = sorted({c["value"] for c in contours if c["value"] >= min_mmi})
    if not levels:
        return None
    level = levels[0]
    polys = []
    for c in contours:
        if c["value"] != level:
            continue
        g = c["geometry"]
        if g["type"] == "Polygon":
            polys.append(g["coordinates"])
        elif g["type"] == "MultiPolygon":
            polys += g["coordinates"]
        else:
            lines = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
            for line in lines:
                ring = [[round(p[0], 4), round(p[1], 4)] for p in line if len(p) >= 2]
                if len(ring) < 3:
                    continue
                if ring[0] != ring[-1]:
                    ring.append(ring[0])
                polys.append([ring])
    if not polys:
        return None
    return {"type": "MultiPolygon", "coordinates": polys, "mmi": level}


# ------------------------------------------------------------------------- reference POIs

# site kind -> POI groups that can locate it
KIND_GROUPS = {"airport": {"airport", "airbase"}, "airbase": {"airbase", "airport"},
               "port": {"port", "naval_base"}, "naval_base": {"naval_base", "port"},
               "military_base": {"military", "airbase", "naval_base"},
               "refinery": {"refinery"}, "oil_depot": {"refinery"},
               "power_plant": {"nuclear_power"}}
_BASE_GROUP = {"airbase": "airbase", "naval_base": "naval_base", "port": "port",
               "refinery": "refinery", "garrison": "military", "radar": "military",
               "air_defense": "military", "missile_site": "military", "nuclear": "nuclear"}


class ReferencePOIs:
    """Facilities from the repo's reference datasets as one list of
    {name, alt, group, lat, lon, country, source}; loaded lazily, missing files ok."""

    def __init__(self, items: list[dict] | None = None) -> None:
        self._items = items

    @property
    def items(self) -> list[dict]:
        if self._items is None:
            self._items = self._load()
        return self._items

    @staticmethod
    def _load() -> list[dict]:
        out: list[dict] = []
        try:
            from osint.reference.loader import get_reference

            ref = get_reference()
            for a in ref.airports:
                out.append({"name": a["name"], "alt": " ".join(
                    x for x in (a.get("municipality"), a.get("iata"), a.get("icao")) if x),
                    "municipality": a.get("municipality") or "",
                    "group": "airbase" if a.get("is_military") else "airport",
                    "lat": a["lat"], "lon": a["lon"], "country": a.get("iso_country") or "",
                    "source": "OurAirports"})
            for p in ref.ports:
                out.append({"name": p["name"], "alt": "", "municipality": p["name"],
                            "group": "port", "lat": p["lat"], "lon": p["lon"],
                            "country": "", "source": p.get("source") or "ports"})
            for b in ref.bases:
                grp = _BASE_GROUP.get(b.get("type") or "")
                if grp:
                    out.append({"name": b["name"], "alt": "", "municipality": "",
                                "group": grp, "lat": b["lat"], "lon": b["lon"],
                                "country": "", "source": "Curated military bases"})
        except Exception:
            log.exception("reference POIs unavailable")
        try:
            from osint.reference.nuclear import get_nuclear

            for x in get_nuclear().items:
                out.append({"name": x["name"], "alt": x.get("name_native") or "",
                            "municipality": "",
                            "group": "nuclear_power" if x["type"] == "power" else "nuclear",
                            "lat": x["lat"], "lon": x["lon"], "country": "",
                            "source": x.get("source") or "nuclear sites"})
        except Exception:
            log.exception("nuclear sites unavailable")
        return out

    def match(self, site: dict, anchor: dict) -> dict | None:
        """Best POI for ``site`` near ``anchor`` ({lat, lon, precision, country,
        place_name}). Named sites need a name match; a bare 'airport' / 'port' resolves
        only to the single facility of that group serving the anchor city."""
        groups = KIND_GROUPS.get(site.get("kind") or "")
        if not groups or anchor.get("lat") is None:
            return None
        limit = ANCHOR_KM.get(anchor.get("precision") or "")
        if limit is None:
            return None  # country-level anchor: too vague to pick a facility
        cands = []
        for p in self.items:
            if p["group"] not in groups:
                continue
            if anchor.get("country") and p["country"] and p["country"] != anchor["country"]:
                continue
            d = haversine_km(anchor["lat"], anchor["lon"], p["lat"], p["lon"])
            if d <= limit:
                cands.append((p, d))
        if not cands:
            return None
        name = site.get("name") or ""
        scored = []
        for p, d in cands:
            s = max(name_match(name, p["name"]), name_match(name, p["name"] + " " + p["alt"]))
            if s > 0:
                scored.append((s, -d, p, d))
        if scored:
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            s, _, p, d = scored[0]
            if len(scored) > 1 and scored[1][0] == s and abs(scored[1][3] - d) > 5:
                return None  # two equally good, distinct facilities: ambiguous
            return {"poi": p, "score": s, "distance_km": round(d, 1), "bare": False}
        if distinctive(name):
            return None  # named, but no facility of that name: let OSM try
        city = fold((anchor.get("place_name") or "").split(",")[0]).strip()
        if not city:
            return None
        local = [(p, d) for p, d in cands if d <= 40 and city and city in fold(
            p.get("municipality") or "")]
        if len(local) == 1:
            return {"poi": local[0][0], "score": 0.6, "distance_km": round(local[0][1], 1),
                    "bare": True}
        return None


# ------------------------------------------------------------------------- cascade


def _bbox_around(lat: float, lon: float, deg: float) -> tuple[float, float, float, float]:
    dlon = deg / max(0.2, math.cos(math.radians(lat)))
    return (max(-180.0, lon - dlon), max(-90.0, lat - deg), min(180.0, lon + dlon),
            min(90.0, lat + deg))


def _split_context(context: str | None) -> tuple[str, str]:
    parts = [p.strip() for p in (context or "").split(",") if p.strip()]
    return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")


class ImpactResolver:
    """Runs the cascade for one event's extracted sites."""

    def __init__(self, *, pois: ReferencePOIs, nominatim: Nominatim | None,
                 geocode: Callable | None) -> None:
        self.pois, self.nominatim, self.geocode = pois, nominatim, geocode

    def site_anchor(self, site: dict, event_anchor: dict) -> dict:
        """The site's own context city (when stated, geocoded in the event's country and
        near the event) else the event's location."""
        place, admin1 = _split_context(site.get("context"))
        country = event_anchor.get("country") or ""
        if place and country and self.geocode is not None:
            try:
                hit = self.geocode(place, admin1, country)
            except Exception:  # noqa: BLE001 - a geocoder failure never stops the cascade
                hit = None
            if hit is not None and hit[3] in ("city", "point"):
                lat, lon, cc, _prec = hit
                ok = event_anchor.get("lat") is None or haversine_km(
                    lat, lon, event_anchor["lat"], event_anchor["lon"]) <= 300 or \
                    event_anchor.get("precision") in ("admin1", "country")
                if ok and (not cc or cc == country):
                    return {"lat": lat, "lon": lon, "precision": "city", "country": country,
                            "place_name": place}
        return dict(event_anchor)

    async def resolve(self, site: dict, event_anchor: dict) -> dict:
        anchor = self.site_anchor(site, event_anchor)
        base = {"name": site.get("name"), "kind": site.get("kind") or "other",
                "context": site.get("context"), "quote": site.get("quote"),
                "anchor": {k: anchor.get(k) for k in ("lat", "lon", "precision",
                                                      "place_name")}}
        # a. coordinates written in the article
        if site.get("coords_stated") and site.get("lat") is not None:
            lat, lon = float(site["lat"]), float(site["lon"])
            limit = {"admin1": 400.0, "country": 1500.0}.get(anchor.get("precision"), 150.0)
            if anchor.get("lat") is None or haversine_km(
                    lat, lon, anchor["lat"], anchor["lon"]) <= limit:
                return {**base, "lat": lat, "lon": lon, "precision": "exact",
                        "confidence": 0.9, "resolver": "stated_coordinates",
                        "source": "coordinates stated in the article"}
        kind = site.get("kind") or "other"
        # (kind city: the name is a town -> gazetteer)
        if kind in ("city",) and self.geocode is not None and anchor.get("country"):
            try:
                hit = self.geocode(site.get("name") or "", "", anchor["country"])
            except Exception:  # noqa: BLE001 - a geocoder failure never stops the cascade
                hit = None
            if hit is not None and hit[3] in ("city", "point") and (
                    event_anchor.get("lat") is None or haversine_km(
                        hit[0], hit[1], event_anchor["lat"], event_anchor["lon"]) <= 400
                    or event_anchor.get("precision") in ("admin1", "country")):
                return {**base, "lat": hit[0], "lon": hit[1], "precision": "city",
                        "confidence": 0.5, "resolver": "gazetteer",
                        "source": "GeoNames gazetteer"}
        # b. reference POIs
        m = self.pois.match(site, anchor)
        if m is not None:
            p = m["poi"]
            return {**base, "lat": p["lat"], "lon": p["lon"], "precision": "facility",
                    "confidence": 0.6 if m["bare"] else round(0.7 + 0.15 * m["score"], 2),
                    "resolver": "reference", "source": p["source"],
                    "matched": p["name"], "distance_km": m["distance_km"]}
        # c. Nominatim (bounded to the anchor's surroundings)
        hit = await self._nominatim(site, anchor)
        if hit is not None:
            return {**base, **hit}
        # d. city / event location
        if anchor.get("lat") is not None:
            prec = anchor.get("precision") or "city"
            prec = "city" if prec in ("point", "exact") else prec
            return {**base, "lat": anchor["lat"], "lon": anchor["lon"], "precision": prec,
                    "confidence": 0.4 if prec == "city" else 0.2, "resolver": "fallback",
                    "source": "event / context city location (site not located)"}
        return {**base, "lat": None, "lon": None, "precision": "unknown",
                "confidence": 0.0, "resolver": "none", "source": "not located"}

    async def _nominatim(self, site: dict, anchor: dict) -> dict | None:
        if self.nominatim is None or anchor.get("lat") is None:
            return None
        deg = VIEWBOX_DEG.get(anchor.get("precision") or "")
        if deg is None or not distinctive(site.get("name") or ""):
            return None  # country-level anchor or a nameless site: never search blindly
        if (site.get("kind") or "") == "city":
            return None
        place = _split_context(site.get("context"))[0] or (
            anchor.get("place_name") or "").split(",")[0]
        q = site["name"] if not place or fold(place) in fold(site["name"]) \
            else f"{site['name']}, {place}"
        vb = _bbox_around(anchor["lat"], anchor["lon"], deg)
        results = await self.nominatim.search(q, countrycode=anchor.get("country"),
                                              viewbox=vb)
        for r in results or []:
            try:
                lat, lon = float(r["lat"]), float(r["lon"])
            except (TypeError, ValueError, KeyError):
                continue
            label = r.get("name") or (r.get("display_name") or "").split(",")[0]
            score = name_match(site["name"], label)
            if score <= 0:
                continue  # matched only the context (e.g. the city itself): reject
            prec = nominatim_precision(r)
            if prec == "city":
                continue
            conf = {"facility": 0.7, "neighbourhood": 0.65, "settlement": 0.65}[prec]
            return {"lat": round(lat, 6), "lon": round(lon, 6), "precision": prec,
                    "confidence": round(conf * (0.85 + 0.15 * score), 2),
                    "resolver": "nominatim",
                    "source": "OpenStreetMap Nominatim (ODbL)", "matched": label,
                    "osm": f"{r.get('osm_type')}/{r.get('osm_id')}",
                    "distance_km": round(haversine_km(anchor["lat"], anchor["lon"],
                                                      lat, lon), 1)}
        return None


def best_site(sites: list[dict]) -> dict | None:
    """The finest, most confident located site (fine precisions only), or None."""
    fine = [s for s in sites if s.get("precision") in FINE and s.get("lat") is not None]
    if not fine:
        return None
    return max(fine, key=lambda s: (PRECISION_RANK[s["precision"]], s.get("confidence") or 0))


def site_area(site: dict) -> dict | None:
    """Uncertainty-radius circle around a located site (None for admin1/country)."""
    prec = site.get("precision")
    if site.get("lat") is None or prec not in AREA_KM and prec != "facility":
        return None
    r = FACILITY_KM.get(site.get("kind") or "", 1.0) if prec == "facility" else AREA_KM[prec]
    label = {"exact": "stated coordinates", "facility": f"facility ({site.get('kind')})",
             "neighbourhood": "neighbourhood", "settlement": "settlement",
             "city": "city"}[prec]
    return {"kind": "uncertainty", "basis": f"uncertainty radius: {label}, {r:g} km",
            "radius_km": r, "center": [site["lon"], site["lat"]], "site": site.get("name"),
            "geometry": circle(site["lat"], site["lon"], r)}


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")
