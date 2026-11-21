"""In-memory 1-degree grid over reference airports for nearest-airport lookups."""

import math

from osint.core.geo import haversine_km

_CELL = 1.0


def _key(lat: float, lon: float) -> tuple[int, int]:
    return math.floor(lat / _CELL), math.floor(lon / _CELL)


class AirportIndex:
    """``airports``: dicts with ident, name, lat, lon (+ optional iata/icao/is_military/type)."""

    def __init__(self, airports: list[dict]) -> None:
        self.grid: dict[tuple[int, int], list[dict]] = {}
        for a in airports:
            if a.get("lat") is None or a.get("lon") is None or not a.get("ident"):
                continue
            self.grid.setdefault(_key(a["lat"], a["lon"]), []).append(a)
        self.size = sum(map(len, self.grid.values()))

    def nearest(self, lat: float, lon: float, radius_km: float) -> tuple[dict, float] | None:
        """Nearest airport within ``radius_km`` (searches enough cells for the radius)."""
        dlat = math.ceil(radius_km / 111.0 / _CELL)
        coslat = max(0.05, math.cos(math.radians(min(89.0, abs(lat)))))
        dlon = min(180, math.ceil(radius_km / (111.0 * coslat) / _CELL))
        ci, cj = _key(lat, lon)
        best, best_d = None, radius_km
        n_lon = int(360 / _CELL)
        for i in range(ci - dlat, ci + dlat + 1):
            for j in range(cj - dlon, cj + dlon + 1):
                jj = (j + n_lon // 2) % n_lon - n_lon // 2  # wrap the antimeridian
                for a in self.grid.get((i, jj), ()):
                    d = haversine_km(lat, lon, a["lat"], a["lon"])
                    if d <= best_d:
                        best, best_d = a, d
        return (best, best_d) if best is not None else None


def airport_ref(a: dict | None) -> dict | None:
    """Compact airport description used in API output."""
    if a is None:
        return None
    return {"ident": a["ident"], "name": a.get("name"), "iata": a.get("iata"), "icao": a.get("icao"),
            "lat": a["lat"], "lon": a["lon"], "is_military": bool(a.get("is_military"))}
