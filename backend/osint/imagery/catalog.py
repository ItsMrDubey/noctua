"""Scene catalogue for the imagery viewer: every usable free scene over a site, newest first.

Sources (all anonymous; verified live, see config/imagery.yaml ``viewer`` and the licence
register):

* ``s2``      Sentinel-2 L2A, Element 84 Earth Search (AWS open data COGs), 10 m.
* ``landsat`` Landsat 8/9 Collection 2 L2, Microsoft Planetary Computer, 30 m. (No pan band
              in L2 and Planetary Computer's L1 holds only Landsat 1-5 MSS, so no 15 m
              pan-sharpening; USGS's own bucket is requester-pays.)
* ``s1``      Sentinel-1 IW RTC (radiometrically terrain corrected gamma0, UTM), Planetary
              Computer, 10 m; day / night, through cloud. RTC lags the raw GRD by days.
* ``s1grd``   Sentinel-1 IW GRD acquisitions newer than the newest RTC (not yet terrain
              corrected; rendered by Planetary Computer's tiler from its GCPs; uncalibrated DN).
* ``maxar``   Maxar Open Data Program (static STAC on S3): 0.3-0.5 m, only for the disaster
              events Maxar released (CC BY-NC 4.0).
* ``wayback`` Esri World Imagery Wayback: the archive releases whose imagery actually
              changed at the site, with the capture date / sensor / resolution from each
              release's metadata service. Viewed as Esri tiles in the browser (not exported).

Each scene is a dict (see :func:`scene`), registered by ``key`` so the renderer can find
its assets again; searches are cached per site and day range.
"""

import json
import logging
import math
import posixpath
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

EARTH_SEARCH = "https://earth-search.aws.element84.com/v1"
PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_DATA = "https://planetarycomputer.microsoft.com/api/data/v1"
MAXAR_ROOT = "https://maxar-opendata.s3.amazonaws.com/events/"
WAYBACK_CONFIG = "https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json"
WAYBACK_TILEMAP = ("https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/"
                   "MapServer/tilemap/{release}/{z}/{row}/{col}")
WAYBACK_TILES = ("https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/"
                 "1.0.0/default028mm/MapServer/tile/{release}/{z}/{y}/{x}")
WAYBACK_Z = 17            # tile level whose history is walked (~300 m tiles)
WAYBACK_META_LAYER = 5    # "60cm Resolution Metadata" (map levels ~17-18)
WAYBACK_MAX_STEPS = 40
WAYBACK_META_TIMEOUT_S = 30.0
WAYBACK_DISK_TTL_S = 7 * 86400  # releases come out about monthly

COPERNICUS = "Contains modified Copernicus Sentinel data {year}"

# source id -> description shown in the viewer (and the licence register)
SOURCES: dict[str, dict] = {
    "s2": {"name": "Sentinel-2 L2A", "sensor": "MSI", "kind": "optical", "gsd_m": 10.0,
           "provider": "Element 84 Earth Search (AWS Open Data)", "licence": "copernicus",
           "presets": ["true", "swir", "nir", "nbr", "dnbr"]},
    "landsat": {"name": "Landsat 8/9 C2 L2", "sensor": "OLI", "kind": "optical", "gsd_m": 30.0,
                "provider": "Microsoft Planetary Computer", "licence": "landsat",
                "presets": ["true", "swir", "nir", "nbr", "dnbr"]},
    "s1": {"name": "Sentinel-1 RTC", "sensor": "C-SAR IW", "kind": "sar", "gsd_m": 10.0,
           "provider": "Microsoft Planetary Computer", "licence": "copernicus",
           "presets": ["sar", "sarvh", "sar_rgb", "sar_change"]},
    "s1grd": {"name": "Sentinel-1 GRD (not terrain corrected)", "sensor": "C-SAR IW",
              "kind": "sar", "gsd_m": 10.0, "provider": "Microsoft Planetary Computer",
              "licence": "copernicus", "presets": ["sar", "sarvh", "sar_rgb", "sar_change"]},
    "maxar": {"name": "Maxar Open Data", "sensor": "WorldView / GeoEye", "kind": "vhr",
              "gsd_m": 0.5, "provider": "Maxar Open Data Program (AWS)", "licence": "maxar_open",
              "presets": ["true"]},
    "wayback": {"name": "Esri World Imagery Wayback", "sensor": "(Esri Wayback mosaic)",
                "kind": "basemap", "gsd_m": 0.5, "provider": "Esri", "licence": "esri_wayback",
                "presets": ["true"]},
}
DEFAULT_SOURCES = ("s2", "landsat", "s1", "s1grd", "maxar", "wayback")
ATTRIBUTION = {
    "copernicus": COPERNICUS,
    "landsat": "Landsat imagery courtesy of the U.S. Geological Survey",
    "maxar_open": "Maxar Open Data Program, CC BY-NC 4.0 (imagery (c) Maxar Technologies)",
    "esri_wayback": "Esri World Imagery Wayback; imagery (c) {provider}",
}


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(raw: str) -> datetime:
    dt = datetime.fromisoformat(str(raw).replace(" ", "T"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _href(assets: dict, key: str) -> str | None:
    a = assets.get(key)
    return a.get("href") if isinstance(a, dict) else None


def _scale(assets: dict, key: str) -> tuple[float, float]:
    a = assets.get(key) or {}
    rb = (a.get("raster:bands") or [{}])[0] if isinstance(a, dict) else {}
    return float(rb.get("scale", 1.0) or 1.0), float(rb.get("offset", 0.0) or 0.0)


def _platform(raw: str | None) -> str:
    """"sentinel-2b" -> "Sentinel-2B", "landsat-9" -> "Landsat-9"."""
    p = str(raw or "").strip()
    if not p:
        return ""
    head, sep, tail = p.rpartition("-")
    return (head[:1].upper() + head[1:] + sep + tail.upper()) if sep else p[:1].upper() + p[1:]


def attribution(source: str, acquired_at: str | None, provider: str | None = None) -> str:
    lic = SOURCES[source]["licence"]
    return ATTRIBUTION[lic].format(year=(acquired_at or "")[:4], provider=provider or "Esri")


def scene(source: str, sid: str, collection: str, acquired_at: str, *, platform: str = "",
          cloud_pct: float | None = None, gsd_m: float | None = None, bbox=None,
          assets: dict | None = None, extra: dict | None = None, renderer: str = "cog") -> dict:
    meta = SOURCES[source]
    return {
        "key": f"{source}:{sid}", "source": source, "id": sid, "collection": collection,
        "name": meta["name"], "platform": platform, "sensor": meta["sensor"],
        "kind": meta["kind"], "acquired_at": acquired_at, "cloud_pct": cloud_pct,
        "gsd_m": gsd_m if gsd_m is not None else meta["gsd_m"], "bbox": list(bbox) if bbox else None,
        "presets": list(meta["presets"]), "renderer": renderer,
        "attribution": attribution(source, acquired_at, (extra or {}).get("provider")),
        "licence": meta["licence"], "assets": assets or {}, "extra": extra or {},
    }


def public(s: dict) -> dict:
    """Scene as sent to the browser (asset URLs and scale factors stay server-side)."""
    out = {k: v for k, v in s.items() if k not in ("assets", "alts")}
    if s.get("alts"):  # other tiles of the same pass (a stored crop may name one of them)
        out["alt_keys"] = [f"{s['source']}:{a['id']}" for a in s["alts"]]
    return out


# ------------------------------------------------------------------ STAC (Earth Search / PC)

def stac_search(client: httpx.Client, url: str, collection: str, lat: float, lon: float,
                start: datetime, end: datetime, limit: int = 250, query: dict | None = None,
                max_pages: int = 3) -> list[dict]:
    body = {"collections": [collection], "intersects": {"type": "Point", "coordinates": [lon, lat]},
            "datetime": f"{iso(start)}/{iso(end)}", "limit": min(limit, 250),
            "sortby": [{"field": "properties.datetime", "direction": "desc"}]}
    if query:
        body["query"] = query
    feats: list[dict] = []
    nxt: tuple[str, dict] | None = (f"{url.rstrip('/')}/search", body)
    for _ in range(max_pages):
        if nxt is None or len(feats) >= limit:
            break
        r = client.post(nxt[0], json=nxt[1])
        r.raise_for_status()
        j = r.json()
        feats += j.get("features") or []
        nxt = None
        for link in j.get("links") or []:
            if link.get("rel") == "next" and link.get("method", "GET").upper() == "POST":
                nxt = (link["href"], {**body, **(link.get("body") or {})})
    return feats[:limit]


def _inside_depth(bbox, lat: float, lon: float) -> float:
    """How far (deg) the point lies inside a bbox (negative = outside)."""
    if not bbox or len(bbox) < 4:
        return 0.0
    w, s, e, n = bbox[:4]
    return min(lon - w, e - lon, lat - s, n - lat)


def _alt(s: dict) -> dict:
    return {"id": s["id"], "collection": s["collection"], "assets": s["assets"],
            "epsg": s["extra"].get("epsg")}


def _dedupe(scenes: list[dict], lat: float, lon: float, within_s: float = 180.0) -> list[dict]:
    """One scene per platform and pass (overlapping S2 / Landsat tiles of the same pass are
    seconds apart): keep the tile the site lies deepest inside."""
    out: list[dict] = []
    for s in sorted(scenes, key=lambda x: (x["platform"], x["acquired_at"])):
        prev = out[-1] if out else None
        if (prev and prev["platform"] == s["platform"]
                and abs((parse_dt(s["acquired_at"]) - parse_dt(prev["acquired_at"])).total_seconds())
                <= within_s):
            win, lose = ((s, prev) if _inside_depth(s["bbox"], lat, lon)
                         > _inside_depth(prev["bbox"], lat, lon) else (prev, s))
            # the other tile of the pass fills the window where the chosen one has no data
            win["alts"] = [*win.get("alts", []), _alt(lose), *lose.get("alts", [])]
            out[-1] = win
            continue
        out.append(s)
    return out


S2_BANDS = ("red", "green", "blue", "nir", "nir08", "swir16", "swir22", "scl")


def parse_s2(f: dict) -> dict | None:
    p, a = f.get("properties") or {}, f.get("assets") or {}
    try:
        when = iso(parse_dt(p["datetime"]))
    except (KeyError, ValueError):
        return None
    hrefs = {k: h for k in S2_BANDS if (h := _href(a, k)) and h.startswith("http")}
    if not all(k in hrefs for k in ("red", "green", "blue")):
        return None
    return scene("s2", f["id"], f.get("collection", "sentinel-2-l2a"), when,
                 platform=_platform(p.get("platform")), cloud_pct=p.get("eo:cloud_cover"),
                 bbox=f.get("bbox"), assets={k: {"href": h, "scale_offset": _scale(a, k)}
                                             for k, h in hrefs.items()},
                 extra={"tile": p.get("s2:mgrs_tile") or p.get("grid:code"),
                        "epsg": p.get("proj:epsg"), "baseline": p.get("s2:processing_baseline"),
                        "thumbnail": _href(a, "thumbnail")})


def search_s2(client, lat, lon, start, end) -> list[dict]:
    feats = stac_search(client, EARTH_SEARCH, "sentinel-2-l2a", lat, lon, start, end)
    return _dedupe([s for f in feats if (s := parse_s2(f))], lat, lon)


LANDSAT_BANDS = ("red", "green", "blue", "nir08", "swir16", "swir22")


def parse_landsat(f: dict) -> dict | None:
    p, a = f.get("properties") or {}, f.get("assets") or {}
    try:
        when = iso(parse_dt(p["datetime"]))
    except (KeyError, ValueError):
        return None
    return scene("landsat", f["id"], f.get("collection", "landsat-c2-l2"), when,
                 platform=_platform(p.get("platform")), cloud_pct=p.get("eo:cloud_cover"),
                 gsd_m=30.0, bbox=f.get("bbox"), renderer="titiler",
                 assets={k: {"scale_offset": _scale(a, k)} for k in LANDSAT_BANDS},
                 extra={"path_row": f"{p.get('landsat:wrs_path')}/{p.get('landsat:wrs_row')}",
                        "sun_elev_deg": p.get("view:sun_elevation"),
                        "off_nadir_deg": p.get("view:off_nadir")})


def search_landsat(client, lat, lon, start, end) -> list[dict]:
    feats = stac_search(client, PC_STAC, "landsat-c2-l2", lat, lon, start, end,
                        query={"platform": {"in": ["landsat-8", "landsat-9"]}})
    return _dedupe([s for f in feats if (s := parse_landsat(f))], lat, lon)


def parse_s1(f: dict, source: str = "s1") -> dict | None:
    p = f.get("properties") or {}
    try:
        when = iso(parse_dt(p["datetime"]))
    except (KeyError, ValueError):
        return None
    pols = [x.lower() for x in p.get("sar:polarizations") or []]
    if "vv" not in pols:
        return None
    s = scene(source, f["id"], f.get("collection", "sentinel-1-rtc"), when,
              platform=_platform(p.get("platform")), bbox=f.get("bbox"), renderer="titiler",
              extra={"orbit": p.get("sat:orbit_state"),
                     "rel_orbit": p.get("sat:relative_orbit"), "polarizations": pols,
                     "mode": p.get("sar:instrument_mode"),
                     "calibrated": source == "s1"})
    if "vh" not in pols:
        s["presets"] = [x for x in s["presets"] if x not in ("sarvh", "sar_rgb")]
    return s


def search_s1(client, lat, lon, start, end) -> tuple[list[dict], list[dict]]:
    """(RTC scenes, GRD-only scenes newer than the newest RTC)."""
    rtc = [s for f in stac_search(client, PC_STAC, "sentinel-1-rtc", lat, lon, start, end)
           if (s := parse_s1(f, "s1"))]
    newest = max((s["acquired_at"] for s in rtc), default="")
    grd_start = max(start, parse_dt(newest)) if newest else start
    grd = [s for f in stac_search(client, PC_STAC, "sentinel-1-grd", lat, lon, grd_start, end,
                                  limit=40)
           if (s := parse_s1(f, "s1grd")) and s["acquired_at"] > newest]
    return rtc, grd


# ------------------------------------------------------------------ Maxar Open Data

class MaxarIndex:
    """Lazy index of the Maxar Open Data static catalogue: event collections (55-odd, cached
    a day) -> acquisition collections -> items, walked only for events whose extent covers
    the site."""

    def __init__(self, ttl_s: float = 24 * 3600) -> None:
        self.ttl_s = ttl_s
        self._events: tuple[float, list[dict]] | None = None
        self._items: dict[str, tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()

    def _json(self, client: httpx.Client, url: str) -> dict:
        r = client.get(url)
        r.raise_for_status()
        return r.json()

    def events(self, client: httpx.Client) -> list[dict]:
        with self._lock:
            if self._events and time.monotonic() - self._events[0] < self.ttl_s:
                return self._events[1]
        root = self._json(client, MAXAR_ROOT + "catalog.json")
        hrefs = [posixpath.normpath(lk["href"]) for lk in root.get("links") or []
                 if lk.get("rel") == "child"]

        def one(h: str) -> dict | None:
            try:
                c = self._json(client, MAXAR_ROOT + h)
            except (httpx.HTTPError, ValueError):
                return None
            ext = c.get("extent") or {}
            return {"href": h, "id": c.get("id") or h.split("/")[0], "title": c.get("title"),
                    "bboxes": (ext.get("spatial") or {}).get("bbox") or [],
                    "interval": ((ext.get("temporal") or {}).get("interval") or [[None, None]])[0],
                    "children": [lk["href"] for lk in c.get("links") or [] if lk.get("rel") == "child"]}

        with ThreadPoolExecutor(12) as ex:
            evs = [e for e in ex.map(one, hrefs) if e]
        with self._lock:
            self._events = (time.monotonic(), evs)
        return evs

    def event_items(self, client: httpx.Client, ev: dict) -> list[dict]:
        with self._lock:
            hit = self._items.get(ev["href"])
            if hit and time.monotonic() - hit[0] < self.ttl_s:
                return hit[1]
        base = posixpath.dirname(ev["href"])
        acq_urls = [posixpath.normpath(posixpath.join(base, h)) for h in ev["children"]]

        def acq(u: str) -> list[str]:
            try:
                c = self._json(client, MAXAR_ROOT + u)
            except (httpx.HTTPError, ValueError):
                return []
            d = posixpath.dirname(u)
            return [posixpath.normpath(posixpath.join(d, lk["href"]))
                    for lk in c.get("links") or [] if lk.get("rel") == "item"]

        def item(u: str) -> dict | None:
            try:
                it = self._json(client, MAXAR_ROOT + u)
            except (httpx.HTTPError, ValueError):
                return None
            it["_path"] = u
            return it

        with ThreadPoolExecutor(16) as ex:
            item_urls = [u for lst in ex.map(acq, acq_urls) for u in lst]
            items = [i for i in ex.map(item, item_urls) if i]
        with self._lock:
            self._items[ev["href"]] = (time.monotonic(), items)
        return items

    def search(self, client: httpx.Client, lat: float, lon: float) -> list[dict]:
        out = []
        for ev in self.events(client):
            if not any(_inside_depth(b, lat, lon) >= 0 for b in ev["bboxes"][:1] or ev["bboxes"]):
                continue
            for it in self.event_items(client, ev):
                if _inside_depth(it.get("bbox"), lat, lon) < 0:
                    continue
                s = parse_maxar(it, ev)
                if s:  # archive: every release over the site, whatever the time window
                    out.append(s)
        return out


def parse_maxar(it: dict, ev: dict) -> dict | None:
    p, a = it.get("properties") or {}, it.get("assets") or {}
    vis = _href(a, "visual")
    if not vis:
        return None
    try:
        when = iso(parse_dt(p["datetime"]))
    except (KeyError, ValueError):
        return None
    base = posixpath.dirname(it["_path"])
    url = vis if vis.startswith("http") else MAXAR_ROOT + posixpath.normpath(posixpath.join(base, vis))
    return scene("maxar", it["_path"].removesuffix(".json"), ev["id"], when,
                 platform=str(p.get("platform") or ""), cloud_pct=p.get("tile:clouds_percent"),
                 gsd_m=float(p.get("gsd") or 0.5), bbox=it.get("bbox"),
                 assets={"visual": {"href": url}},
                 extra={"event": ev["id"], "event_title": ev.get("title"),
                        "catalog_id": p.get("catalog_id"), "epsg": p.get("proj:epsg"),
                        "off_nadir_deg": p.get("view:off_nadir"),
                        "sun_elev_deg": p.get("view:sun_elevation"),
                        "data_km2": p.get("tile:data_area")})


# ------------------------------------------------------------------ Esri Wayback

def tile_xy(lat: float, lon: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


class Wayback:
    def __init__(self, ttl_s: float = 24 * 3600, cache_dir: Path | None = None) -> None:
        self.ttl_s = ttl_s
        self.cache_dir = cache_dir
        self._cfg: tuple[float, list[dict]] | None = None
        self._lock = threading.Lock()

    def _disk(self, lat: float, lon: float) -> Path | None:
        if self.cache_dir is None:
            return None
        x, y = tile_xy(lat, lon, WAYBACK_Z)
        return self.cache_dir / f"wayback-{WAYBACK_Z}-{x}-{y}.json"

    def releases(self, client: httpx.Client) -> list[dict]:
        """Releases newest first: {release, date, title, metadata}."""
        with self._lock:
            if self._cfg and time.monotonic() - self._cfg[0] < self.ttl_s:
                return self._cfg[1]
        r = client.get(WAYBACK_CONFIG)
        r.raise_for_status()
        out = []
        for k, v in r.json().items():
            title = v.get("itemTitle") or ""
            date = title.rsplit(" ", 1)[-1].rstrip(")")
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                continue
            out.append({"release": int(k), "date": date, "title": title,
                        "metadata": v.get("metadataLayerUrl")})
        out.sort(key=lambda x: x["date"], reverse=True)
        with self._lock:
            self._cfg = (time.monotonic(), out)
        return out

    def changes(self, client: httpx.Client, lat: float, lon: float) -> list[dict]:
        """Releases in which the imagery of the site's level-17 tile changed (newest first),
        found by walking the tilemap service: each release reports the release its tile
        was last updated in."""
        rel = self.releases(client)
        idx = {r["release"]: i for i, r in enumerate(rel)}
        x, y = tile_xy(lat, lon, WAYBACK_Z)
        seen: list[dict] = []
        i = 0
        for _ in range(WAYBACK_MAX_STEPS):
            if i >= len(rel):
                break
            r = client.get(WAYBACK_TILEMAP.format(release=rel[i]["release"], z=WAYBACK_Z,
                                                  row=y, col=x))
            r.raise_for_status()
            j = r.json()
            if not (j.get("data") or [0])[0]:
                break
            sel = (j.get("select") or [rel[i]["release"]])[0]
            at = idx.get(sel, i)
            seen.append(rel[at])
            i = at + 1
        return seen

    def metadata(self, client: httpx.Client, rel: dict, lat: float, lon: float) -> dict:
        if not rel.get("metadata"):
            return {}
        q = urlencode({"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint",
                       "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
                       "outFields": "SRC_DATE,SRC_RES,SRC_ACC,SRC_DESC,NICE_DESC,NICE_NAME",
                       "returnGeometry": "false", "f": "json"})
        # the metadata services answer in 0.3-20 s; a slow / failed one leaves that release
        # listed by its release date only (capture date unknown)
        try:
            r = client.get(f"{rel['metadata']}/{WAYBACK_META_LAYER}/query?{q}",
                           timeout=httpx.Timeout(WAYBACK_META_TIMEOUT_S, connect=10))
            r.raise_for_status()
            feats = r.json().get("features") or []
        except (httpx.HTTPError, ValueError) as exc:
            log.info("wayback metadata %s: %s", rel["release"], exc)
            return {}
        return (feats[0].get("attributes") or {}) if feats else {}

    def search(self, client: httpx.Client, lat: float, lon: float) -> list[dict]:
        """Captures at the site, cached on disk per level-17 tile for a week (the metadata
        services are slow)."""
        path = self._disk(lat, lon)
        if path is not None and path.is_file():
            try:
                if time.time() - path.stat().st_mtime < WAYBACK_DISK_TTL_S:
                    return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        out = self._search(client, lat, lon)
        if path is not None and out:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(out), encoding="utf-8")
            except OSError:
                log.debug("wayback cache write failed", exc_info=True)
        return out

    def _search(self, client: httpx.Client, lat: float, lon: float) -> list[dict]:
        rels = self.changes(client, lat, lon)
        with ThreadPoolExecutor(8) as ex:
            metas = list(ex.map(lambda r: self.metadata(client, r, lat, lon), rels))
        out: list[dict] = []
        by_capture: dict[tuple, dict] = {}
        for rel, m in zip(rels, metas, strict=True):
            sd = str(m.get("SRC_DATE") or "")
            captured = (f"{sd[:4]}-{sd[4:6]}-{sd[6:8]}T00:00:00Z" if len(sd) == 8
                        else rel["date"] + "T00:00:00Z")
            ident = (sd, m.get("SRC_DESC"), m.get("SRC_RES")) if sd else ("release", rel["release"])
            if ident in by_capture:  # same capture re-released (reprocessed / re-mosaicked)
                by_capture[ident]["extra"]["first_release_date"] = rel["date"]
                continue
            provider = m.get("NICE_DESC") or "Esri"
            s = scene("wayback", str(rel["release"]), "world-imagery-wayback", captured,
                      platform=str(m.get("SRC_DESC") or ""), gsd_m=m.get("SRC_RES"),
                      renderer="tiles",
                      extra={"release": rel["release"], "release_date": rel["date"],
                             "first_release_date": rel["date"],
                             "capture_date_known": len(sd) == 8, "provider": provider,
                             "product": m.get("NICE_NAME"), "accuracy_m": m.get("SRC_ACC"),
                             "tiles": WAYBACK_TILES.replace("{release}", str(rel["release"]))})
            by_capture[ident] = s
            out.append(s)  # archive: all captures at the site, any capture date
        return out


# ------------------------------------------------------------------ catalogue service

class Catalogue:
    """Parallel search over the sources, per-site cache, scene registry for the renderer."""

    def __init__(self, client_factory, ttl_s: float = 15 * 60, registry_max: int = 20000,
                 cache_dir: Path | None = None) -> None:
        self.client_factory = client_factory
        self.ttl_s = ttl_s
        self.maxar = MaxarIndex()
        self.wayback = Wayback(cache_dir=cache_dir)
        self._cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
        self._reg: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()
        self.registry_max = registry_max

    def register(self, scenes: list[dict]) -> None:
        with self._lock:
            for s in scenes:
                self._reg[s["key"]] = s
                self._reg.move_to_end(s["key"])
            while len(self._reg) > self.registry_max:
                self._reg.popitem(last=False)

    def get(self, key: str) -> dict | None:
        with self._lock:
            return self._reg.get(key)

    def _run(self, src: str, client, lat, lon, start, end) -> list[dict]:
        if src == "s2":
            return search_s2(client, lat, lon, start, end)
        if src == "landsat":
            return search_landsat(client, lat, lon, start, end)
        if src == "s1":
            rtc, grd = search_s1(client, lat, lon, start, end)
            return rtc + grd
        # the archives (Maxar events, Wayback releases) are listed whatever the time window
        if src == "maxar":
            return self.maxar.search(client, lat, lon)
        if src == "wayback":
            return self.wayback.search(client, lat, lon)
        return []

    def search(self, lat: float, lon: float, start: datetime, end: datetime,
               sources: tuple[str, ...] = DEFAULT_SOURCES) -> dict:
        srcs = tuple(s for s in sources if s in SOURCES and s != "s1grd")
        key = (round(lat, 3), round(lon, 3), start.strftime("%Y%m%d"), end.strftime("%Y%m%d%H"),
               srcs)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl_s:
                return {**hit[1], "cached": True}
        t0 = time.perf_counter()
        status: dict[str, dict] = {}

        def one(src: str) -> list[dict]:
            t = time.perf_counter()
            try:
                with self.client_factory() as client:
                    found = self._run(src, client, lat, lon, start, end)
                status[src] = {"count": len(found), "ms": round((time.perf_counter() - t) * 1000)}
                return found
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                log.warning("imagery catalogue %s failed: %s", src, exc)
                status[src] = {"count": 0, "error": f"{type(exc).__name__}: {exc}"[:300],
                               "ms": round((time.perf_counter() - t) * 1000)}
                return []

        with ThreadPoolExecutor(len(srcs) or 1) as ex:
            scenes = [s for lst in ex.map(one, srcs) for s in lst]
        if "s1" in status:  # split the GRD-only tail into its own source line
            grd = sum(1 for s in scenes if s["source"] == "s1grd")
            status["s1"]["count"] -= grd
            status["s1grd"] = {"count": grd, "ms": status["s1"]["ms"]}
        scenes.sort(key=lambda s: s["acquired_at"], reverse=True)
        self.register(scenes)
        out = {"site": {"lat": lat, "lon": lon}, "window": {"from": iso(start), "to": iso(end)},
               "scenes": [public(s) for s in scenes],
               "sources": [{"id": k, **{x: SOURCES[k][x] for x in ("name", "sensor", "kind", "gsd_m", "provider", "licence")},
                            **status.get(k, {"skipped": True})} for k in SOURCES
                           if k in status or k in srcs],
               "took_ms": round((time.perf_counter() - t0) * 1000), "cached": False}
        # a source that failed is retried after a minute, not after the full TTL
        stamp = time.monotonic() - (self.ttl_s - 60 if any("error" in v for v in status.values()) else 0)
        with self._lock:
            self._cache[key] = (stamp, out)
            while len(self._cache) > 128:
                self._cache.popitem(last=False)
        return out
