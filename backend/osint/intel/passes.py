"""Overhead imaging opportunities: when public Earth-observation satellites can image a site.

Orbits come from the CelesTrak GP element sets the satellites collector already caches
(``signals/satellites.json``), propagated with SGP4 (the ``sgp4`` package, vectorised).
For each satellite we find every closest approach of its ground track to the site in the
window, refine it to ~1 s, and test it against the sensor's footprint:

* optical (Sentinel-2 MSI, 290 km swath; Landsat 8/9 OLI, 185 km): the site within half a
  swath of the ground track and the sun at least ``MIN_SUN_ELEV`` above the horizon there
  (these sensors image the day side; night passes are dropped).
* SAR (Sentinel-1 C-band, Interferometric Wide swath): right-looking, incidence 29.1-46.0
  deg, i.e. the site to the right of the ground track at the ground range those incidence
  angles give for the satellite's altitude. Day or night, through cloud.

What this is and is not: a *geometric opportunity*. Whether the sensor actually records
the scene depends on the mission's acquisition plan (Sentinel-1's observation scenario,
Sentinel-2's land/coastal mask, Landsat's WRS-2 schedule) and the satellite's status; none
of that is modelled. SGP4 on a fresh element set is good to ~1-3 km along track per day of
element age, i.e. seconds in time; ``time_unc_s`` reports that estimate per pass.
"""

import logging
import math
import re
from datetime import UTC, datetime

import numpy as np

from osint.intel.astro import sun_position

log = logging.getLogger(__name__)

R_EQ = 6378.137
F_WGS = 1 / 298.257223563
E2 = F_WGS * (2 - F_WGS)
R_MEAN = 6371.0088
MIN_SUN_ELEV = 10.0
COARSE_S = 30.0
MAX_TRACK_KM = 1200.0  # closest approaches farther than this can't be in any footprint

# name pattern -> sensor model; matched against the CelesTrak object name
SENSORS = (
    (re.compile(r"^SENTINEL-2[A-Z]$"), {"mission": "Sentinel-2", "sensor": "MSI", "kind": "optical",
                                       "half_swath_km": 145.0, "res": "10 m",
                                       "archive": "Copernicus Data Space"}),
    (re.compile(r"^LANDSAT [89]$"), {"mission": "Landsat", "sensor": "OLI", "kind": "optical",
                                     "half_swath_km": 92.5, "res": "15/30 m",
                                     "archive": "USGS EarthExplorer"}),
    (re.compile(r"^SENTINEL-1[A-Z]$"), {"mission": "Sentinel-1", "sensor": "C-SAR IW", "kind": "sar",
                                       "incidence": (29.1, 46.0), "res": "5x20 m",
                                       "archive": "Copernicus Data Space"}),
)


def sensor_for(name: str) -> dict | None:
    for rx, model in SENSORS:
        if rx.match(name.strip().upper()):
            return model
    return None


def pick_satellites(objects: dict) -> list[tuple[list, dict]]:
    """(compact GP row, sensor model) for every tracked EO satellite in the cache."""
    out = []
    for row in (objects or {}).values():
        try:
            model = sensor_for(str(row[1]))
        except (IndexError, TypeError):
            continue
        if model:
            out.append((row, model))
    out.sort(key=lambda x: str(x[0][1]))
    return out


def satrec(row: list):
    """sgp4 Satrec from a compact CelesTrak row (osint.signals.satellites.FIELDS)."""
    from sgp4 import omm
    from sgp4.api import Satrec

    norad, name, intl, epoch, mm, ecc, inc, raan, argp, ma, bstar, mmdot, mmddot = row[:13]
    fields = {"OBJECT_NAME": name, "OBJECT_ID": intl, "EPOCH": epoch, "MEAN_MOTION": mm,
              "ECCENTRICITY": ecc, "INCLINATION": inc, "RA_OF_ASC_NODE": raan,
              "ARG_OF_PERICENTER": argp, "MEAN_ANOMALY": ma, "EPHEMERIS_TYPE": 0,
              "CLASSIFICATION_TYPE": "U", "NORAD_CAT_ID": norad, "ELEMENT_SET_NO": 999,
              "REV_AT_EPOCH": 0, "BSTAR": bstar, "MEAN_MOTION_DOT": mmdot,
              "MEAN_MOTION_DDOT": mmddot}
    sat = Satrec()
    omm.initialize(sat, {k: str(v) for k, v in fields.items()})
    return sat


def _epoch(row: list) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(row[3]).replace("Z", ""))
    except ValueError:
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t


def _jd(ts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    days = ts / 86400.0 + 2440587.5
    jd = np.floor(days)
    return jd, days - jd


def _gmst(ts: np.ndarray) -> np.ndarray:
    """Greenwich mean sidereal time (rad), IAU 1982, from unix seconds (UT1 ~ UTC)."""
    d = ts / 86400.0 + 2440587.5 - 2451545.0
    T = d / 36525.0
    g = 280.46061837 + 360.98564736629 * d + 0.000387933 * T * T - T ** 3 / 38710000.0
    return np.radians(np.mod(g, 360.0))


def ecef(sat, ts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Earth-fixed positions (N x 3, km) and an error mask at unix times ``ts``."""
    jd, fr = _jd(ts)
    err, r, _ = sat.sgp4_array(jd, fr)
    th = _gmst(ts)
    c, s = np.cos(th), np.sin(th)
    x = c * r[:, 0] + s * r[:, 1]
    y = -s * r[:, 0] + c * r[:, 1]
    return np.stack([x, y, r[:, 2]], axis=1), err != 0


def site_ecef(lat: float, lon: float) -> np.ndarray:
    phi, lam = math.radians(lat), math.radians(lon)
    n = R_EQ / math.sqrt(1 - E2 * math.sin(phi) ** 2)
    return np.array([n * math.cos(phi) * math.cos(lam), n * math.cos(phi) * math.sin(lam),
                     n * (1 - E2) * math.sin(phi)])


def subpoint(p: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Geodetic lat / lon (deg) and height (km) of ECEF points (Bowring, 2 iterations)."""
    x, y, z = p[..., 0], p[..., 1], p[..., 2]
    lon = np.arctan2(y, x)
    rho = np.hypot(x, y)
    lat = np.arctan2(z, rho * (1 - E2))
    for _ in range(2):
        n = R_EQ / np.sqrt(1 - E2 * np.sin(lat) ** 2)
        lat = np.arctan2(z + E2 * n * np.sin(lat), rho)
    n = R_EQ / np.sqrt(1 - E2 * np.sin(lat) ** 2)
    h = rho / np.cos(lat) - n
    return np.degrees(lat), np.degrees(lon), h


def ground_km(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2
    return 2 * R_MEAN * np.arcsin(np.minimum(1.0, np.sqrt(a)))


def ground_range_km(incidence_deg: float, alt_km: float) -> float:
    """Ground distance from nadir to where the beam meets the ground at ``incidence_deg``."""
    th = math.radians(incidence_deg)
    look = math.asin(R_MEAN * math.sin(th) / (R_MEAN + alt_km))
    return R_MEAN * (th - look)


def off_nadir_deg(cross_km: float, alt_km: float) -> float:
    """Off-nadir view angle for a ground offset ``cross_km`` from the ground track."""
    a = cross_km / R_MEAN
    return math.degrees(math.atan2(R_MEAN * math.sin(a), R_MEAN + alt_km - R_MEAN * math.cos(a)))


def _closest(sat, lat: float, lon: float, ts: np.ndarray) -> list[float]:
    """Unix times of local minima of ground-track distance to the site (coarse grid)."""
    p, bad = ecef(sat, ts)
    la, lo, _ = subpoint(p)
    d = ground_km(lat, lon, la, lo)
    d[bad] = np.inf
    idx = np.where((d[1:-1] <= d[:-2]) & (d[1:-1] < d[2:]) & (d[1:-1] < MAX_TRACK_KM))[0] + 1
    return [float(ts[i]) for i in idx]


def _refine(sat, lat: float, lon: float, t: float) -> dict | None:
    ts = np.arange(t - COARSE_S, t + COARSE_S + 0.5, 0.5)
    p, bad = ecef(sat, ts)
    if bad.all():
        return None
    la, lo, h = subpoint(p)
    d = ground_km(lat, lon, la, lo)
    d[bad] = np.inf
    i = int(np.argmin(d))
    i0, i1 = max(0, i - 2), min(len(ts) - 1, i + 2)
    v = p[i1] - p[i0]  # direction of motion (earth-fixed)
    up = p[i] / np.linalg.norm(p[i])
    right = np.cross(v, up)
    side = "right" if float(np.dot(right, site_ecef(lat, lon) - p[i])) > 0 else "left"
    # ascending when the sub-satellite latitude increases
    asc = bool(la[i1] > la[i0])
    return {"t": float(ts[i]), "cross_km": float(d[i]), "alt_km": float(h[i]), "side": side,
            "node": "ascending" if asc else "descending"}


def predict(rows: list[tuple[list, dict]], lat: float, lon: float, start: datetime,
            end: datetime, now: datetime | None = None) -> list[dict]:
    """Every footprint-covering pass of the given satellites over (lat, lon) in [start, end],
    sorted by time. Each: {t, satellite, norad, mission, sensor, kind, cross_km, side,
    off_nadir_deg | incidence_deg, sun_elev_deg, node, element_age_h, time_unc_s, ...}."""
    ts = np.arange(start.timestamp(), end.timestamp() + COARSE_S, COARSE_S)
    out = []
    for row, model in rows:
        try:
            sat = satrec(row)
        except Exception:  # one malformed element set skips one satellite
            log.debug("bad element set %s", row[:2], exc_info=True)
            continue
        epoch = _epoch(row)
        for t in _closest(sat, lat, lon, ts):
            ca = _refine(sat, lat, lon, t)
            if ca is None:
                continue
            when = datetime.fromtimestamp(ca["t"], UTC)
            sun = sun_position(when, lat, lon)[0]
            rec = {"t": when.isoformat(timespec="seconds"), "satellite": str(row[1]),
                   "norad": int(row[0]), "mission": model["mission"], "sensor": model["sensor"],
                   "kind": model["kind"], "res": model["res"], "archive": model["archive"],
                   "cross_km": round(ca["cross_km"], 1), "side": ca["side"],
                   "node": ca["node"], "alt_km": round(ca["alt_km"]),
                   "sun_elev_deg": round(sun, 1)}
            if model["kind"] == "optical":
                if ca["cross_km"] > model["half_swath_km"] or sun < MIN_SUN_ELEV:
                    continue
                rec["off_nadir_deg"] = round(off_nadir_deg(ca["cross_km"], ca["alt_km"]), 1)
            else:
                near_km = ground_range_km(model["incidence"][0], ca["alt_km"])
                far_km = ground_range_km(model["incidence"][1], ca["alt_km"])
                if ca["side"] != "right" or not near_km <= ca["cross_km"] <= far_km:
                    continue
                # incidence at the site from the ground range (inverse of ground_range_km)
                a = ca["cross_km"] / R_MEAN
                look = math.atan2(R_MEAN * math.sin(a), R_MEAN + ca["alt_km"] - R_MEAN * math.cos(a))
                rec["incidence_deg"] = round(math.degrees(look + a), 1)
            if epoch is not None:
                age_d = abs((when - epoch).total_seconds()) / 86400
                rec["element_age_h"] = round(age_d * 24, 1)
                # ~1 km + 2 km/day along-track error; ground speed ~6.8 km/s
                rec["time_unc_s"] = max(1, round((1.0 + 2.0 * age_d) / 6.8))
            out.append(rec)
    out.sort(key=lambda r: r["t"])
    return out
