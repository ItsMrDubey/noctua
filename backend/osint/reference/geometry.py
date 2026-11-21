"""Small geodesy helpers."""

import math

R_EARTH_M = 6371008.8
FT = 0.3048


def runway_rect(lat1: float, lon1: float, lat2: float, lon2: float, width_ft: float) -> list[list[float]]:
    """Four [lon, lat] corners of a runway of given width around its centreline."""
    lat0 = math.radians((lat1 + lat2) / 2)
    mx = math.cos(lat0) * R_EARTH_M * math.pi / 180  # metres per degree lon
    my = R_EARTH_M * math.pi / 180
    dx = (lon2 - lon1) * mx
    dy = (lat2 - lat1) * my
    ln = math.hypot(dx, dy)
    if ln == 0:
        return []
    half = max(width_ft, 60.0) * FT / 2  # keep tiny strips visible
    nx, ny = -dy / ln * half, dx / ln * half
    pts = [(lon1, lat1, 1), (lon2, lat2, 1), (lon2, lat2, -1), (lon1, lat1, -1)]
    return [[round(lo + s * nx / mx, 6), round(la + s * ny / my, 6)] for lo, la, s in pts]


def in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float] | None) -> bool:
    if bbox is None:
        return True
    w, s, e, n = bbox
    if not s <= lat <= n:
        return False
    return w <= lon <= e if w <= e else (lon >= w or lon <= e)  # w > e: crosses antimeridian
