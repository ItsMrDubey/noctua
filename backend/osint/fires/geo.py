"""Grid indexes for the fire scorer: points within a radius, distance to polylines and
point-in-polygon. Pure Python, bucketed by fixed-degree cells, so a lookup touches only a
few cells (tens of thousands of hotspots per pass stay cheap). Distances use a local
equirectangular projection, accurate to well under 1% at the <= 50 km radii used here."""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from collections.abc import Iterable, Iterator

KM_PER_DEG = 111.195


def _kx(lat: float) -> float:
    """km per degree of longitude at ``lat``."""
    return KM_PER_DEG * max(0.01, math.cos(math.radians(lat)))


def local_km(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """(x, y) km of (lat, lon) relative to (lat0, lon0); longitude wraps the antimeridian."""
    dlon = (lon - lon0 + 540.0) % 360.0 - 180.0
    return dlon * _kx(lat0), (lat - lat0) * KM_PER_DEG


def dist_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    x, y = local_km(lat1, lon1, lat2, lon2)
    return math.hypot(x, y)


def seg_dist_km(lat: float, lon: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance from (lat, lon) to segment a-b ((lat, lon) pairs)."""
    ax, ay = local_km(lat, lon, a[0], a[1])
    bx, by = local_km(lat, lon, b[0], b[1])
    dx, dy = bx - ax, by - ay
    den = dx * dx + dy * dy
    t = 0.0 if den == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / den))
    return math.hypot(ax + t * dx, ay + t * dy)


class _Grid:
    def __init__(self, cell_deg: float) -> None:
        self.deg = cell_deg
        self.ncols = max(1, round(360.0 / cell_deg))
        self.cells: dict[tuple[int, int], list] = defaultdict(list)

    def key(self, lat: float, lon: float) -> tuple[int, int]:
        return math.floor(lat / self.deg), math.floor((lon + 180.0) / self.deg) % self.ncols

    def around(self, lat: float, lon: float, km: float) -> Iterator[list]:
        cy, cx = self.key(lat, lon)
        ry = math.ceil(km / KM_PER_DEG / self.deg)
        rx = min(self.ncols // 2, math.ceil(km / _kx(min(89.0, abs(lat) + ry * self.deg))
                                            / self.deg))
        cells = self.cells
        for y in range(cy - ry, cy + ry + 1):
            for x in range(cx - rx, cx + rx + 1):
                bucket = cells.get((y, x % self.ncols))
                if bucket:
                    yield bucket


class PointIndex(_Grid):
    """Points (lat, lon, payload) bucketed by cell."""

    def __init__(self, points: Iterable[tuple[float, float, object]] = (),
                 cell_deg: float = 0.25) -> None:
        super().__init__(cell_deg)
        self.n = 0
        for lat, lon, payload in points:
            self.add(lat, lon, payload)

    def add(self, lat: float, lon: float, payload: object = None) -> None:
        self.cells[self.key(lat, lon)].append((lat, lon, payload))
        self.n += 1

    def __len__(self) -> int:
        return self.n

    def within(self, lat: float, lon: float, km: float) -> list[tuple[float, object]]:
        """[(distance_km, payload)] of the points within ``km``, nearest first."""
        out = []
        for bucket in self.around(lat, lon, km):
            for la, lo, p in bucket:
                d = dist_km(lat, lon, la, lo)
                if d <= km:
                    out.append((d, p))
        out.sort(key=lambda x: x[0])
        return out

    def count(self, lat: float, lon: float, km: float) -> int:
        n = 0
        kx, km2 = _kx(lat), km * km
        for bucket in self.around(lat, lon, km):
            for la, lo, _ in bucket:
                dx = ((lo - lon + 540.0) % 360.0 - 180.0) * kx
                dy = (la - lat) * KM_PER_DEG
                if dx * dx + dy * dy <= km2:
                    n += 1
        return n

    def nearest(self, lat: float, lon: float, km: float) -> tuple[float, object] | None:
        hits = self.within(lat, lon, km)
        return hits[0] if hits else None


class SegmentIndex(_Grid):
    """Polyline segments bucketed into every cell their bounding box touches."""

    def __init__(self, lines: Iterable[list[tuple[float, float]]] = (),
                 cell_deg: float = 0.1) -> None:
        super().__init__(cell_deg)
        self.n = 0
        for line in lines:
            for a, b in itertools.pairwise(line):
                self.add(a, b)

    def add(self, a: tuple[float, float], b: tuple[float, float]) -> None:
        y0, x0 = self.key(min(a[0], b[0]), min(a[1], b[1]))
        y1, x1 = self.key(max(a[0], b[0]), max(a[1], b[1]))
        if x1 < x0:  # crosses the antimeridian: index both ends only (rare, short segments)
            for k in {self.key(*a), self.key(*b)}:
                self.cells[k].append((a, b))
        else:
            for y in range(y0, y1 + 1):
                for x in range(x0, x1 + 1):
                    self.cells[(y, x)].append((a, b))
        self.n += 1

    def __len__(self) -> int:
        return self.n

    def distance(self, lat: float, lon: float, max_km: float) -> float | None:
        """Distance to the nearest segment, or None when none is within ``max_km``."""
        best = None
        for bucket in self.around(lat, lon, max_km):
            for a, b in bucket:
                d = seg_dist_km(lat, lon, a, b)
                if d <= max_km and (best is None or d < best):
                    best = d
        return best


class PolygonIndex:
    """Point-in-polygon over many polygons (GeoJSON rings [[lon, lat], ...], first ring
    outer, the rest holes). Edges are bucketed into latitude bands, so the horizontal ray
    of a query only meets the few edges crossing its band. ``contains`` is true inside any
    polygon (holes excluded)."""

    def __init__(self, polygons: Iterable[list[list[list[float]]]] = (),
                 band_deg: float = 0.02) -> None:
        self.band = band_deg
        self.bands: dict[int, list[tuple[int, float, float, float, float]]] = defaultdict(list)
        self.n = 0
        for poly in polygons:
            self.add(poly)

    def add(self, rings: list[list[list[float]]]) -> None:
        pid = self.n
        self.n += 1
        for ring in rings:
            for (x1, y1, *_), (x2, y2, *_) in itertools.pairwise(ring):
                if y1 == y2:
                    continue
                for b in range(math.floor(min(y1, y2) / self.band),
                               math.floor(max(y1, y2) / self.band) + 1):
                    self.bands[b].append((pid, x1, y1, x2, y2))

    def __len__(self) -> int:
        return self.n

    def contains(self, lat: float, lon: float) -> bool:
        odd: set[int] = set()
        for pid, x1, y1, x2, y2 in self.bands.get(math.floor(lat / self.band), ()):
            if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                odd ^= {pid}
        return bool(odd)


def geojson_polygons(geom: dict | None) -> list[list[list[list[float]]]]:
    """Polygon / MultiPolygon geometry -> list of polygons (rings of [lon, lat])."""
    if not geom:
        return []
    if geom.get("type") == "Polygon":
        return [geom.get("coordinates") or []]
    if geom.get("type") == "MultiPolygon":
        return list(geom.get("coordinates") or [])
    return []
