"""Sun and moon at a site, computed locally (no network).

* Sun: the NOAA solar-position algorithm (Meeus-based; elevation within ~0.01 deg for
  1900-2100). Elevations are geometric (no refraction), so "sunrise" here is the moment the
  centre of the disc crosses -0.833 deg, the usual refraction + semi-diameter convention.
* Moon: a truncated Meeus series for ecliptic longitude / latitude (~0.3 deg), enough for the
  illuminated fraction (~1 %), phase name and whether the moon is above the horizon.

Used by the intel pane's local-conditions tile: light at event time (day / twilight /
night), next sunrise / sunset, and how much moonlight a night-time event had.
"""

import math
from datetime import UTC, datetime, timedelta

RAD = math.pi / 180.0
SUNRISE_ELEV = -0.833


def julian_day(t: datetime) -> float:
    return t.astimezone(UTC).timestamp() / 86400.0 + 2440587.5


def _sun_ecliptic(jd: float) -> tuple[float, float, float]:
    """(apparent longitude deg, obliquity deg, equation of time minutes)."""
    T = (jd - 2451545.0) / 36525.0
    L0 = (280.46646 + T * (36000.76983 + 0.0003032 * T)) % 360
    M = 357.52911 + T * (35999.05029 - 0.0001537 * T)
    e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T)
    C = (math.sin(M * RAD) * (1.914602 - T * (0.004817 + 0.000014 * T))
         + math.sin(2 * M * RAD) * (0.019993 - 0.000101 * T) + math.sin(3 * M * RAD) * 0.000289)
    omega = 125.04 - 1934.136 * T
    lam = L0 + C - 0.00569 - 0.00478 * math.sin(omega * RAD)
    eps0 = 23 + (26 + (21.448 - T * (46.815 + T * (0.00059 - T * 0.001813))) / 60) / 60
    eps = eps0 + 0.00256 * math.cos(omega * RAD)
    y = math.tan(eps * RAD / 2) ** 2
    eot = 4 / RAD * (y * math.sin(2 * L0 * RAD) - 2 * e * math.sin(M * RAD)
                     + 4 * e * y * math.sin(M * RAD) * math.cos(2 * L0 * RAD)
                     - 0.5 * y * y * math.sin(4 * L0 * RAD) - 1.25 * e * e * math.sin(2 * M * RAD))
    return lam, eps, eot


def sun_position(t: datetime, lat: float, lon: float) -> tuple[float, float]:
    """(elevation deg, azimuth deg from north) of the sun's centre, geometric."""
    jd = julian_day(t)
    lam, eps, eot = _sun_ecliptic(jd)
    decl = math.asin(math.sin(eps * RAD) * math.sin(lam * RAD))
    u = t.astimezone(UTC)
    minutes = u.hour * 60 + u.minute + u.second / 60 + u.microsecond / 6e7
    tst = (minutes + eot + 4 * lon) % 1440
    ha = (tst / 4 - 180) * RAD
    phi = lat * RAD
    cz = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    zen = math.acos(max(-1.0, min(1.0, cz)))
    az = math.atan2(math.sin(ha), math.cos(ha) * math.sin(phi) - math.tan(decl) * math.cos(phi))
    return 90 - zen / RAD, (az / RAD + 180) % 360


def light_phase(elev: float) -> str:
    """day / civil / nautical / astronomical twilight / night from the sun's elevation."""
    if elev >= SUNRISE_ELEV:
        return "day"
    if elev >= -6:
        return "civil twilight"
    if elev >= -12:
        return "nautical twilight"
    if elev >= -18:
        return "astronomical twilight"
    return "night"


def _crossings(t0: datetime, hours: float, lat: float, lon: float, level: float,
               step_min: int = 4) -> list[tuple[datetime, str]]:
    """Times in [t0, t0 + hours] at which the sun crosses ``level`` ('rise' / 'set'),
    found on a coarse grid and refined by bisection to ~1 s."""
    out: list[tuple[datetime, str]] = []
    n = int(hours * 60 / step_min)
    prev_t, prev_e = t0, sun_position(t0, lat, lon)[0] - level
    for i in range(1, n + 1):
        t = t0 + timedelta(minutes=i * step_min)
        e = sun_position(t, lat, lon)[0] - level
        if (prev_e < 0) != (e < 0):
            a, b, ea = prev_t, t, prev_e
            for _ in range(18):
                m = a + (b - a) / 2
                em = sun_position(m, lat, lon)[0] - level
                if (ea < 0) == (em < 0):
                    a, ea = m, em
                else:
                    b = m
            out.append((a + (b - a) / 2, "rise" if e > 0 else "set"))
        prev_t, prev_e = t, e
    return out


def sun_events(t: datetime, lat: float, lon: float, hours: float = 30) -> dict:
    """Next sunrise / sunset and civil dawn / dusk after ``t`` (None in polar day / night)."""
    nxt = {}
    for level, rise, set_ in ((SUNRISE_ELEV, "sunrise", "sunset"),
                              (-6.0, "civil_dawn", "civil_dusk")):
        for when, kind in _crossings(t, hours, lat, lon, level):
            key = rise if kind == "rise" else set_
            nxt.setdefault(key, when)
    return {k: nxt.get(k) for k in ("sunrise", "sunset", "civil_dawn", "civil_dusk")}


def moon_position(t: datetime) -> tuple[float, float, float]:
    """(ecliptic longitude deg, latitude deg, distance km), truncated Meeus ch. 47."""
    d = julian_day(t) - 2451545.0
    T = d / 36525.0
    Lp = 218.3164477 + 481267.88123421 * T
    D = (297.8501921 + 445267.1114034 * T) * RAD
    M = (357.5291092 + 35999.0502909 * T) * RAD
    Mp = (134.9633964 + 477198.8675055 * T) * RAD
    F = (93.2720950 + 483202.0175233 * T) * RAD
    lon = (Lp + 6.288774 * math.sin(Mp) + 1.274027 * math.sin(2 * D - Mp)
           + 0.658314 * math.sin(2 * D) + 0.213618 * math.sin(2 * Mp)
           - 0.185116 * math.sin(M) - 0.114332 * math.sin(2 * F)
           + 0.058793 * math.sin(2 * D - 2 * Mp) + 0.057066 * math.sin(2 * D - M - Mp)
           + 0.053322 * math.sin(2 * D + Mp) + 0.045758 * math.sin(2 * D - M)) % 360
    lat = (5.128122 * math.sin(F) + 0.280602 * math.sin(Mp + F)
           + 0.277693 * math.sin(Mp - F) + 0.173237 * math.sin(2 * D - F))
    dist = 385000.56 - 20905.355 * math.cos(Mp) - 3699.111 * math.cos(2 * D - Mp) \
        - 2955.968 * math.cos(2 * D)
    return lon, lat, dist


PHASES = ("new moon", "waxing crescent", "first quarter", "waxing gibbous", "full moon",
          "waning gibbous", "last quarter", "waning crescent")


def moon_phase(t: datetime) -> dict:
    """Illuminated fraction 0..1, phase angle, elongation and the phase name."""
    mlon, mlat, _ = moon_position(t)
    slon = _sun_ecliptic(julian_day(t))[0]
    dl = (mlon - slon) % 360  # 0 new, 180 full; < 180 waxing
    cos_psi = math.cos(mlat * RAD) * math.cos(dl * RAD)
    psi = math.acos(max(-1.0, min(1.0, cos_psi)))
    # phase angle ~ 180 - elongation (the sun is ~400x farther than the moon)
    frac = (1 - cos_psi) / 2
    name = PHASES[int(((dl + 22.5) % 360) // 45)]
    return {"illumination": round(frac, 3), "elongation_deg": round(psi / RAD, 1),
            "age_deg": round(dl, 1), "phase": name}


def moon_elevation(t: datetime, lat: float, lon: float) -> float:
    """Apparent elevation of the moon's centre (deg, ~1 deg; no refraction)."""
    mlon, mlat, _ = moon_position(t)
    jd = julian_day(t)
    eps = _sun_ecliptic(jd)[1] * RAD
    lam, beta = mlon * RAD, mlat * RAD
    ra = math.atan2(math.sin(lam) * math.cos(eps) - math.tan(beta) * math.sin(eps), math.cos(lam))
    dec = math.asin(math.sin(beta) * math.cos(eps) + math.cos(beta) * math.sin(eps) * math.sin(lam))
    gmst = (280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360
    ha = (gmst + lon) * RAD - ra
    phi = lat * RAD
    s = math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(ha)
    e = math.asin(max(-1.0, min(1.0, s))) / RAD
    return e - 0.95 * math.cos(e * RAD)  # horizontal parallax (~0.95 deg) lowers the moon


def light_at(t: datetime, lat: float, lon: float) -> dict:
    """Light conditions at the site at ``t``: sun elevation / azimuth / phase, moon."""
    el, az = sun_position(t, lat, lon)
    m = moon_phase(t)
    mel = moon_elevation(t, lat, lon)
    return {"t": t.astimezone(UTC).isoformat(timespec="seconds"), "sun_elev_deg": round(el, 1),
            "sun_az_deg": round(az), "phase": light_phase(el), "moon": {
                **m, "elev_deg": round(mel, 1), "up": mel > 0}}
