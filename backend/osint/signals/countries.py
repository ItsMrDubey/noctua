"""Country label points and simplified outlines by ISO 3166-1 alpha-2 code, from the Natural
Earth 1:50m admin-0 file the reference fetch script downloads (``data/reference``). Missing
file -> no countries (outage rows without a known code are then skipped)."""

from __future__ import annotations

import threading
from pathlib import Path

from osint.reference.loader import _geojson, default_dir

FILE = "ne_50m_admin_0_countries.geojson"


def _ring(coords: list, nd: int) -> list[float]:
    """Flat [lon, lat, ...] ring rounded to ``nd`` decimals, consecutive repeats removed."""
    out: list[float] = []
    for pt in coords:
        lon, lat = round(pt[0], nd), round(pt[1], nd)
        if len(out) >= 2 and out[-2] == lon and out[-1] == lat:
            continue
        out += [lon, lat]
    return out


class Countries:
    def __init__(self, path: Path | None = None, decimals: int = 2) -> None:
        self.path = path or default_dir() / FILE
        self.decimals = decimals
        self.points: dict[str, tuple[float, float, str]] = {}  # cc -> (lat, lon, name)
        self.shapes: dict[str, list[list[list[float]]]] = {}  # cc -> polygons -> rings
        self._load()

    def _load(self) -> None:
        feats = (_geojson(self.path) or {}).get("features") or []
        primary, fallback = [], []
        for f in feats:
            p = f.get("properties") or {}
            cc = str(p.get("ISO_A2") or "")
            (primary if len(cc) == 2 else fallback).append(f)
        for f in primary + fallback:
            p = f.get("properties") or {}
            cc = str(p.get("ISO_A2") or "")
            if len(cc) != 2:
                cc = str(p.get("ISO_A2_EH") or "")
                if len(cc) != 2 or cc in self.points:
                    continue  # -99 or a territory sharing its parent's code
            lon, lat = p.get("LABEL_X"), p.get("LABEL_Y")
            if lon is None or lat is None or cc in self.points:
                continue
            self.points[cc] = (float(lat), float(lon), str(p.get("NAME") or cc))
            geom = f.get("geometry") or {}
            polys = geom.get("coordinates") or []
            if geom.get("type") == "Polygon":
                polys = [polys]
            elif geom.get("type") != "MultiPolygon":
                polys = []
            self.shapes[cc] = [[_ring(r, self.decimals) for r in poly] for poly in polys]

    def point(self, cc: str) -> tuple[float, float, str] | None:
        return self.points.get(cc.upper())

    def shape(self, cc: str) -> list[list[list[float]]]:
        return self.shapes.get(cc.upper(), [])


_lock = threading.Lock()
_cache: Countries | None = None


def get_countries() -> Countries:
    global _cache
    with _lock:
        if _cache is None:
            _cache = Countries()
        return _cache
