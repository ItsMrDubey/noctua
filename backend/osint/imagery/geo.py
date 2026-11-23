"""Pure-Python UTM (transverse Mercator, Kruger series, WGS84) and crop window math."""

import math

A = 6378137.0
F = 1 / 298.257223563
K0 = 0.9996
_N = F / (2 - F)
_AA = A / (1 + _N) * (1 + _N**2 / 4 + _N**4 / 64)
_ALPHA = (
    _N / 2 - 2 * _N**2 / 3 + 5 * _N**3 / 16,
    13 * _N**2 / 48 - 3 * _N**3 / 5,
    61 * _N**3 / 240,
)


def utm_zone_params(epsg: int) -> tuple[float, bool]:
    """(central meridian in degrees, southern hemisphere) for EPSG 326zz / 327zz."""
    if 32601 <= epsg <= 32660:
        zone, south = epsg - 32600, False
    elif 32701 <= epsg <= 32760:
        zone, south = epsg - 32700, True
    else:
        raise ValueError(f"not a WGS84 UTM EPSG code: {epsg}")
    return (zone - 1) * 6 - 180 + 3.0, south


def utm_forward(lat: float, lon: float, epsg: int) -> tuple[float, float]:
    """WGS84 lat/lon (deg) -> (easting, northing) in metres for the given UTM EPSG."""
    lon0, south = utm_zone_params(epsg)
    phi = math.radians(lat)
    dl = math.radians(((lon - lon0 + 180) % 360) - 180)
    e = 2 * math.sqrt(_N) / (1 + _N)
    sp = math.sin(phi)
    t = math.sinh(math.atanh(sp) - e * math.atanh(e * sp))
    xi = math.atan2(t, math.cos(dl))
    eta = math.atanh(math.sin(dl) / math.sqrt(1 + t * t))
    xs, es = xi, eta
    for j, a in enumerate(_ALPHA, start=1):
        xs += a * math.sin(2 * j * xi) * math.cosh(2 * j * eta)
        es += a * math.cos(2 * j * xi) * math.sinh(2 * j * eta)
    return 500000.0 + K0 * _AA * es, (10000000.0 if south else 0.0) + K0 * _AA * xs


def window_bounds(lat: float, lon: float, half_km: float) -> dict:
    """Approximate lat/lon bbox of a square window centred on the point."""
    dlat = half_km * 1000 / 111320.0
    dlon = half_km * 1000 / (111320.0 * max(0.05, math.cos(math.radians(lat))))
    return {"west": lon - dlon, "south": lat - dlat, "east": lon + dlon, "north": lat + dlat}


def pixel_window(easting: float, northing: float, origin_x: float, origin_y: float,
                 px_x: float, px_y: float, half_m: float) -> tuple[int, int, int, int]:
    """Pixel window (col0, row0, width, height) of a square of side 2*half_m centred on the
    point, for a north-up raster with top-left (origin_x, origin_y) and pixel size px_x/px_y.
    The window is not clamped to the raster; callers pad out-of-raster pixels."""
    size_x = max(1, round(2 * half_m / px_x))
    size_y = max(1, round(2 * half_m / px_y))
    col = (easting - origin_x) / px_x
    row = (origin_y - northing) / px_y
    return round(col - size_x / 2), round(row - size_y / 2), size_x, size_y


def utm_forward_np(lat, lon, epsg: int):
    """Vectorised :func:`utm_forward` over numpy arrays of lat/lon (deg)."""
    import numpy as np

    lon0, south = utm_zone_params(epsg)
    phi = np.radians(lat)
    dl = np.radians(((np.asarray(lon) - lon0 + 180) % 360) - 180)
    e = 2 * math.sqrt(_N) / (1 + _N)
    sp = np.sin(phi)
    t = np.sinh(np.arctanh(sp) - e * np.arctanh(e * sp))
    xi = np.arctan2(t, np.cos(dl))
    eta = np.arctanh(np.sin(dl) / np.sqrt(1 + t * t))
    xs, es = xi.copy(), eta.copy()
    for j, a in enumerate(_ALPHA, start=1):
        xs += a * np.sin(2 * j * xi) * np.cosh(2 * j * eta)
        es += a * np.cos(2 * j * xi) * np.sinh(2 * j * eta)
    return 500000.0 + K0 * _AA * es, (10000000.0 if south else 0.0) + K0 * _AA * xs


def metres_per_degree(lat: float) -> tuple[float, float]:
    """(metres per degree of longitude, of latitude) at ``lat`` (WGS84 ellipsoid)."""
    p = math.radians(lat)
    e2 = F * (2 - F)
    w = math.sqrt(1 - e2 * math.sin(p) ** 2)
    return (math.pi / 180 * A * math.cos(p) / w, math.pi / 180 * A * (1 - e2) / w ** 3)


def bbox_around(lat: float, lon: float, half_km: float) -> tuple[float, float, float, float]:
    """(west, south, east, north) of a square of side 2*half_km centred on the point."""
    mx, my = metres_per_degree(lat)
    dx, dy = half_km * 1000 / max(1.0, mx), half_km * 1000 / my
    return (lon - dx, lat - dy, lon + dx, lat + dy)
