"""Geospatial and time helpers."""

import math
from datetime import UTC, datetime

import h3

H3_RESOLUTIONS = (3, 5, 7)
_EARTH_RADIUS_KM = 6371.0088


def h3_cells(lat: float, lon: float) -> tuple[int, int, int]:
    """H3 cell ids (as unsigned ints) at resolutions 3, 5 and 7."""
    return tuple(h3.str_to_int(h3.latlng_to_cell(lat, lon, r)) for r in H3_RESOLUTIONS)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def valid_coord(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def utcnow() -> datetime:
    return datetime.now(UTC)
