"""Coarse land/ocean mask on H3 res-4, distance-to-coast and distance-to-airport lookups."""

import csv
import json
import logging
import math
import threading
from collections import defaultdict
from pathlib import Path

import h3

from osint.core.geo import haversine_km

log = logging.getLogger(__name__)

RES = 4
# res-4 hexagon: mean edge ~22.6 km; neighbour centres are ~39 km apart; inradius ~19.6 km
RING_STEP_KM = 39.0
HALF_CELL_KM = 19.6
MAX_COAST_KM = 600.0
_AIRPORT_SKIP = {"closed", "balloonport"}


def cell(lat: float, lon: float, res: int = RES) -> str:
    return h3.latlng_to_cell(lat, lon, res)


def centre(c: str) -> tuple[float, float]:
    return h3.cell_to_latlng(c)


def boundary(c: str) -> list[list[float]]:
    """[[lon, lat], ...] closed ring, 3 dp."""
    ring = [[round(lo, 3), round(la, 3)] for la, lo in h3.cell_to_boundary(c)]
    return ring + ring[:1]


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angle_diff(a: float, b: float) -> float:
    """Signed smallest difference b - a in degrees (-180, 180]."""
    d = (b - a + 180.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 else d


def mean_point(points: list[tuple[float, float]],
               weights: list[float] | None = None) -> tuple[float, float]:
    """Weighted spherical mean of (lat, lon) points (antimeridian safe)."""
    weights = weights or [1.0] * len(points)
    x = y = z = 0.0
    for (la, lo), w in zip(points, weights, strict=True):
        p, l_ = math.radians(la), math.radians(lo)
        x += w * math.cos(p) * math.cos(l_)
        y += w * math.cos(p) * math.sin(l_)
        z += w * math.sin(p)
    hyp = math.hypot(x, y)
    return math.degrees(math.atan2(z, hyp)), math.degrees(math.atan2(y, x))


def _rings(geom: dict) -> list[list[list[list[float]]]]:
    if geom.get("type") == "Polygon":
        return [geom["coordinates"]]
    if geom.get("type") == "MultiPolygon":
        return list(geom["coordinates"])
    return []


def land_cells_from_geojson(path: Path, res: int = RES) -> set[str]:
    """Cells whose centre is on land, plus every cell holding a coastline vertex (keeps
    small islands that a centre test would miss)."""
    gj = json.loads(path.read_text(encoding="utf-8"))
    cells: set[str] = set()
    for f in gj.get("features", []):
        for poly in _rings(f.get("geometry") or {}):
            outer = [(la, lo) for lo, la, *_ in poly[0]]
            holes = [[(la, lo) for lo, la, *_ in r] for r in poly[1:]]
            try:
                cells.update(h3.polygon_to_cells(h3.LatLngPoly(outer, *holes), res))
            except Exception:  # noqa: BLE001 - pole-wrapping rings (Antarctica); vertices still count
                log.debug("naval: polygon_to_cells failed for a ring; using its vertices")
            for ring in poly:
                for lo, la, *_ in ring:
                    cells.add(h3.latlng_to_cell(la, lo, res))
    return cells


class OceanMask:
    """Land/ocean classification and approximate distance to the nearest land cell."""

    def __init__(self, land: set[str]) -> None:
        self.land = land
        self._coast: dict[str, float] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_natural_earth(cls, path: Path) -> "OceanMask":
        if not path.is_file():
            log.warning("naval: land polygons missing (%s); every cell treated as ocean", path)
            return cls(set())
        return cls(land_cells_from_geojson(path))

    def is_land_cell(self, c: str) -> bool:
        return c in self.land

    def is_ocean(self, lat: float, lon: float) -> bool:
        return cell(lat, lon) not in self.land

    def coast_km_cell(self, c: str) -> float:
        """Distance from a res-4 cell centre to the nearest land cell edge (approx.)."""
        with self._lock:
            hit = self._coast.get(c)
        if hit is not None:
            return hit
        if c in self.land:
            d = 0.0
        else:
            d = MAX_COAST_KM
            lat, lon = centre(c)
            kmax = int(MAX_COAST_KM / RING_STEP_KM) + 1
            found_at = None
            for k in range(1, kmax + 1):
                ring = [x for x in h3.grid_ring(c, k) if x in self.land]
                if ring:
                    best = min(haversine_km(lat, lon, *centre(x)) for x in ring)
                    d = min(d, max(0.0, best - HALF_CELL_KM))
                    if found_at is None:
                        found_at = k
                if found_at is not None and k >= found_at + 1:
                    break
        with self._lock:
            self._coast[c] = d
        return d

    def coast_km(self, lat: float, lon: float) -> float:
        return self.coast_km_cell(cell(lat, lon))


class AirportIndex:
    """Every airfield / heliport / seaplane base on a 1-degree grid."""

    def __init__(self, points: list[tuple[float, float, str]]) -> None:
        self.grid: dict[tuple[int, int], list[tuple[float, float, str]]] = defaultdict(list)
        for la, lo, ident in points:
            self.grid[(math.floor(la), math.floor(lo))].append((la, lo, ident))
        self.count = len(points)

    @classmethod
    def from_csv(cls, path: Path, extra: list[tuple[float, float, str]] | None = None
                 ) -> "AirportIndex":
        pts: list[tuple[float, float, str]] = list(extra or [])
        if path.is_file():
            with path.open(newline="", encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    if (r.get("type") or "") in _AIRPORT_SKIP:
                        continue
                    try:
                        pts.append((float(r["latitude_deg"]), float(r["longitude_deg"]),
                                    r.get("ident") or ""))
                    except (KeyError, TypeError, ValueError):
                        continue
        else:
            log.warning("naval: airports file missing (%s)", path)
        return cls(pts)

    def nearest_km(self, lat: float, lon: float, within_km: float = 300.0
                   ) -> tuple[float, str | None]:
        """(distance, ident) of the nearest airfield within ``within_km`` (else (inf, None))."""
        dlat = math.ceil(within_km / 111.0)
        coslat = max(0.05, math.cos(math.radians(lat)))
        dlon = min(180, math.ceil(within_km / (111.0 * coslat)))
        best, ident = math.inf, None
        la0, lo0 = math.floor(lat), math.floor(lon)
        for i in range(la0 - dlat, la0 + dlat + 1):
            for j in range(lo0 - dlon, lo0 + dlon + 1):
                jj = (j + 180) % 360 - 180
                for la, lo, idt in self.grid.get((i, jj), ()):
                    d = haversine_km(lat, lon, la, lo)
                    if d < best:
                        best, ident = d, idt
        return (best, ident) if best <= within_km else (math.inf, None)


class Geo:
    """OceanMask + AirportIndex bundle used by the indicators."""

    def __init__(self, mask: OceanMask, airports: AirportIndex) -> None:
        self.mask = mask
        self.airports = airports

    def coast_km(self, lat: float, lon: float) -> float:
        return self.mask.coast_km(lat, lon)

    def open_ocean(self, lat: float, lon: float, min_km: float) -> bool:
        """True when > min_km from land and from any airfield."""
        if self.mask.coast_km(lat, lon) <= min_km:
            return False
        return self.airports.nearest_km(lat, lon, min_km)[1] is None

    @classmethod
    def from_reference(cls, ref_dir: Path) -> "Geo":
        bases: list[tuple[float, float, str]] = []
        try:
            from osint.reference.loader import load_bases

            bases = [(b["lat"], b["lon"], b.get("name", "")) for b in load_bases()
                     if b.get("lat") is not None and b.get("lon") is not None]
        except Exception:  # noqa: BLE001 - curated bases are a nicety
            log.warning("naval: curated bases unavailable")
        return cls(OceanMask.from_natural_earth(ref_dir / "ne_50m_admin_0_countries.geojson"),
                   AirportIndex.from_csv(ref_dir / "airports.csv", bases))


_geo: Geo | None = None
_geo_lock = threading.Lock()


def get_geo(ref_dir: Path | None = None) -> Geo:
    """Process-wide Geo (land mask build takes a few seconds; call from a worker thread)."""
    global _geo
    with _geo_lock:
        if _geo is None:
            from osint.reference.loader import default_dir

            _geo = Geo.from_reference(ref_dir or default_dir())
        return _geo
