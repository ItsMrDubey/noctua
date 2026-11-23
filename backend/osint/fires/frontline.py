"""Ukraine front line from DeepStateMap (https://deepstatemap.live).

``/api/history/last`` returns the current map as GeoJSON: occupied / unknown-status (grey
zone) polygons plus labels, arrows and unrelated political polygons. The front line is not
a feature of its own, so it is derived: an edge of the occupied + grey-zone area is front
line when, ``probe_km`` to one side, the point is occupied and, to the other side, it is
Ukrainian-held land (inside Ukraine's Natural Earth polygon). That drops edges shared by
adjacent occupied polygons, the Russian/Belarusian border and the coast. Without the
Natural Earth countries file every outer edge of the occupied area counts (logged).

The derived lines are cached as ``data/reference/deepstate_frontline.json``; the worker
refreshes it (FireScoreJob), the API only reads it.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path

from osint.core.geo import utcnow
from osint.fires.geo import KM_PER_DEG, PolygonIndex, dist_km, geojson_polygons

log = logging.getLogger(__name__)

CACHE_NAME = "deepstate_frontline.json"
COUNTRIES = "ne_50m_admin_0_countries.geojson"
SOURCE = "DeepStateMap.live"
# feature names are "<uk> /// <en> /// geoJSON.<key>"; these keys make up the occupied area
OCCUPIED_KEYS = ("geoJSON.status.occupied", "geoJSON.status.unknown",
                 "geoJSON.territories.ordlo", "geoJSON.territories.crimea")


def occupied_polygons(fc: dict) -> list[list[list[list[float]]]]:
    out = []
    for f in (fc or {}).get("features") or []:
        name = str((f.get("properties") or {}).get("name") or "")
        if any(k in name for k in OCCUPIED_KEYS):
            out += geojson_polygons(f.get("geometry"))
    return out


def ukraine_polygons(path: Path) -> list[list[list[list[float]]]] | None:
    """Ukraine (ADM0_A3 UKR) from Natural Earth admin-0 countries, None when unavailable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for f in data.get("features") or []:
        p = f.get("properties") or {}
        if "UKR" in (p.get("ADM0_A3"), p.get("ISO_A3")):
            return geojson_polygons(f.get("geometry"))
    return None


def _probe(lat: float, lon: float, nx: float, ny: float, km: float) -> tuple[float, float]:
    kx = KM_PER_DEG * max(0.01, math.cos(math.radians(lat)))
    return lat + ny * km / KM_PER_DEG, lon + nx * km / kx


def derive_lines(occupied: list, ukraine: list | None, probe_km: float = 1.0,
                 margin_km: float = 4.0) -> list[list[list[float]]]:
    """Front-line polylines ([[lon, lat], ...]) from the occupied polygons. The free side
    must be inside Ukraine both at ``probe_km`` and at ``margin_km`` (Natural Earth 1:50m
    borders are a few km off the DeepStateMap ones)."""
    occ = PolygonIndex(occupied)
    ukr = PolygonIndex(ukraine) if ukraine else None
    lines: list[list[list[float]]] = []
    for poly in occupied:
        for ring in poly:
            cur: list[list[float]] = []
            for (x1, y1, *_), (x2, y2, *_) in itertools.pairwise(ring):
                mlat, mlon = (y1 + y2) / 2, (x1 + x2) / 2
                kx = KM_PER_DEG * max(0.01, math.cos(math.radians(mlat)))
                dx, dy = (x2 - x1) * kx, (y2 - y1) * KM_PER_DEG
                length = math.hypot(dx, dy)
                front = False
                if length > 0:
                    nx, ny = -dy / length, dx / length
                    a = _probe(mlat, mlon, nx, ny, probe_km)
                    b = _probe(mlat, mlon, -nx, -ny, probe_km)
                    in_a, in_b = occ.contains(*a), occ.contains(*b)
                    if in_a != in_b:
                        sign = -1.0 if in_a else 1.0
                        free = b if in_a else a
                        far = _probe(mlat, mlon, sign * nx, sign * ny, margin_km)
                        front = ukr is None or (ukr.contains(*free) and ukr.contains(*far))
                if front:
                    if not cur:
                        cur = [[round(x1, 5), round(y1, 5)]]
                    cur.append([round(x2, 5), round(y2, 5)])
                elif cur:
                    lines.append(cur)
                    cur = []
            if cur:
                lines.append(cur)
    return lines


def line_length_km(lines: list[list[list[float]]]) -> float:
    return sum(dist_km(a[1], a[0], b[1], b[0])
               for line in lines for a, b in itertools.pairwise(line))


def build(payload: dict, ref_dir: Path, probe_km: float = 1.0,
          now: datetime | None = None) -> dict:
    """The cache record for one DeepStateMap response."""
    fc = payload.get("map") if isinstance(payload.get("map"), dict) else payload
    occupied = occupied_polygons(fc)
    if not occupied:
        raise ValueError("DeepStateMap response has no occupied polygons")
    ukraine = ukraine_polygons(ref_dir / COUNTRIES)
    if ukraine is None:
        log.warning("front line: %s missing; using every edge of the occupied area",
                    ref_dir / COUNTRIES)
    lines = derive_lines(occupied, ukraine, probe_km)
    return {"source": SOURCE, "source_id": payload.get("id"),
            "source_datetime": payload.get("datetime"),
            "fetched_at": (now or utcnow()).isoformat(timespec="seconds"),
            "clipped_to_ukraine": ukraine is not None,
            "length_km": round(line_length_km(lines), 1), "lines": lines}


def save(record: dict, ref_dir: Path) -> Path:
    ref_dir.mkdir(parents=True, exist_ok=True)
    dest = ref_dir / CACHE_NAME
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, dest)
    return dest


def load(ref_dir: Path) -> dict | None:
    try:
        rec = json.loads((ref_dir / CACHE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and isinstance(rec.get("lines"), list) else None


def lines_latlon(record: dict | None) -> list[list[tuple[float, float]]]:
    """Cached lines as (lat, lon) tuples (SegmentIndex input)."""
    return [[(p[1], p[0]) for p in line] for line in (record or {}).get("lines") or []]


def geojson(record: dict | None) -> dict:
    """The cache as a GeoJSON FeatureCollection (one MultiLineString) for the globe."""
    if not record:
        return {"type": "FeatureCollection", "features": []}
    props = {k: record.get(k) for k in ("source", "source_id", "source_datetime",
                                        "fetched_at", "length_km", "clipped_to_ukraine")}
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": props,
        "geometry": {"type": "MultiLineString", "coordinates": record.get("lines") or []}}]}


async def fetch(http, url: str) -> dict:
    """GET the DeepStateMap map JSON (raises RuntimeError with a short message)."""
    import httpx

    try:
        r = await http.get(url, timeout=httpx.Timeout(60, connect=15))
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"DeepStateMap: HTTP {e.response.status_code}") from None
    except (httpx.HTTPError, ValueError) as e:
        raise RuntimeError(f"DeepStateMap: {type(e).__name__}") from None
    if not isinstance(data, dict):
        raise RuntimeError("DeepStateMap: unexpected response")  # noqa: TRY004 — one error type
    return data
