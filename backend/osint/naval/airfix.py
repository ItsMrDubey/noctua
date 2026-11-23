"""Carrier-aircraft "fixes": launch / recovery / helicopter-loiter signatures reconstructed from
flight legs, each one a point where a ship-borne aircraft plausibly met its ship.

A fix is evidence for a hypothesis, never a ship position. Guards against the usual false
positives: airfields and heliports (OurAirports + curated bases), offshore oil/gas platforms
(VIIRS upstream flare sites as a proxy), land-based maritime patrol / tankers (never a fix),
fighters on training-range sorties from a shore base (down-weighted), JASDF Kawasaki C-2s
(same ICAO type code as the C-2A COD), position glitches (implied speed)."""

import math
import re
from dataclasses import dataclass, field
from datetime import datetime

from osint.core.geo import haversine_km
from osint.naval.geo import Geo, mean_point
from osint.naval.legs import Leg, Pt


@dataclass
class AirFix:
    track_id: str
    ts: datetime
    lat: float
    lon: float
    role: str                 # launch | recovery | loiter
    type_code: str
    callsign: str
    label: str                # human label of the aircraft (type / callsign unit)
    weight: float
    alt_ft: int | None
    speed_kts: float | None
    airfield_km: float        # inf when none within the search radius
    coast_km: float
    reason: str               # one-line explanation
    sigma_km: float = 25.0    # spread of the fix itself around the ship
    path: list[list[float]] = field(default_factory=list)  # [[lon, lat], ...] thinned leg

    def describe(self) -> str:
        what = {"launch": "first fix", "recovery": "last fix", "loiter": "loitered"}[self.role]
        alt = f"{self.alt_ft} ft" if self.alt_ft is not None else "alt n/a"
        spd = f"{self.speed_kts:.0f} kt" if self.speed_kts is not None else "speed n/a"
        af = (f"{self.airfield_km:.0f} km from nearest airfield" if math.isfinite(self.airfield_km)
              else "no airfield within 600 km")
        who = " ".join(x for x in (self.label, self.callsign) if x)
        return f"{who} {what} {alt}, {spd}, {af}, {self.ts:%H:%MZ %d %b}{self.reason}"

    def to_dict(self) -> dict:
        return {"track_id": self.track_id, "ts": self.ts.isoformat(), "lat": round(self.lat, 4),
                "lon": round(self.lon, 4), "role": self.role, "type_code": self.type_code,
                "callsign": self.callsign, "label": self.label, "weight": round(self.weight, 3),
                "alt_ft": self.alt_ft, "speed_kts": self.speed_kts,
                "airfield_km": round(self.airfield_km, 0) if math.isfinite(self.airfield_km) else None,
                "coast_km": round(self.coast_km, 0), "description": self.describe(),
                "path": self.path}


def _hex_in(track_id: str, ranges: list) -> bool:
    try:
        v = int(track_id.split(":")[-1], 16)
    except ValueError:
        return False
    return any(int(lo, 16) <= v <= int(hi, 16) for lo, hi in ranges or [])


class AircraftModel:
    """Type / callsign classification from ``naval.yaml`` (carrier_types + aircraft)."""

    def __init__(self, cfg) -> None:
        self.ac = cfg.sec("aircraft")
        self.mults = {k.upper(): float(v) for k, v in (cfg.sec("carrier_types") or {}).items()}
        self.labels = {k.upper(): v for k, v in (self.ac.get("type_labels") or {}).items()}
        self.support = {t.upper() for t in self.ac.get("support_types") or []}
        self.fighters = {t.upper() for t in self.ac.get("fighter_types") or []}
        lo = self.ac.get("loiter") or {}
        self.rotary = {t.upper() for t in lo.get("types") or []}
        self.hex_exclude = {k.upper(): v for k, v in (self.ac.get("type_hex_exclude") or {}).items()}
        self.callsigns = []
        for c in self.ac.get("callsigns") or []:
            try:
                self.callsigns.append((re.compile(c["pattern"]), c))
            except (re.error, KeyError, TypeError):
                continue

    def callsign_match(self, callsign: str | None) -> dict | None:
        cs = (callsign or "").strip().upper()
        if not cs:
            return None
        for rx, c in self.callsigns:
            if rx.search(cs):
                return c
        return None

    def classify(self, track_id: str, meta: dict) -> tuple[float, str, str]:
        """(multiplier, label, type) for a track; multiplier 0 = not a carrier-fix aircraft."""
        t = (meta.get("type_code") or "").upper().strip()
        if t in self.hex_exclude and _hex_in(track_id, self.hex_exclude[t]):
            return 0.0, f"{t} (excluded: hex block of a non-carrier operator)", t
        mult = self.mults.get(t, 0.0)
        label = self.labels.get(t, t or "military aircraft")
        cs = self.callsign_match(meta.get("name"))
        if cs is not None and float(cs.get("weight", 0)) > mult:
            mult = float(cs["weight"])
        if cs is not None:
            label = f"{label} ({cs.get('label')})"
        if t in self.support:
            return 0.0, label, t
        if mult <= 0:
            mult = float(self.ac.get("generic_weight", 0.0)) if meta.get("is_mil") else 0.0
        return mult, label, t


def _clean(pts: list[Pt], max_kts: float) -> list[Pt]:
    """Drop reports implying an impossible speed from the previous kept one."""
    out: list[Pt] = []
    for p in pts:
        if out:
            q = out[-1]
            dt_h = (p.ts - q.ts).total_seconds() / 3600.0
            if dt_h > 0 and haversine_km(q.lat, q.lon, p.lat, p.lon) / 1.852 / dt_h > max_kts \
                    and haversine_km(q.lat, q.lon, p.lat, p.lon) > 5:
                continue
        out.append(p)
    return out


def _thin(pts: list[Pt], n: int = 40) -> list[list[float]]:
    step = max(1, math.ceil(len(pts) / n))
    sel = pts[::step]
    if sel[-1] is not pts[-1]:
        sel.append(pts[-1])
    return [[round(p.lon, 4), round(p.lat, 4)] for p in sel]


def _vrate(edge: list[Pt]) -> float | None:
    """Mean vertical rate (ft/min) from the extreme report inwards (positive = climbing away
    from the extreme report: for a launch edge = climb after take-off; for a reversed
    recovery edge = how much higher it was before the last report)."""
    a = [p for p in edge if p.alt_ft is not None]
    if len(a) < 2:
        return None
    dt = abs((a[-1].ts - a[0].ts).total_seconds()) / 60.0
    return (a[-1].alt_ft - a[0].alt_ft) / dt if dt > 0 else None


def _edge_fix(leg: Leg, edge: list[Pt], role: str, other: Pt, mult: float, label: str, t: str,
              meta: dict, geo: Geo, ac: dict, model: AircraftModel) -> AirFix | None:
    p = edge[0]
    low_alt = float(ac.get("low_alt_ft", 1500))
    slow = float(ac.get("slow_kts", 220))
    appear = float(ac.get("appear_alt_ft", 2500))
    why = None
    if p.alt_ft is not None and p.alt_ft <= appear:
        why = "low"
    elif any(q.alt_ft is not None and q.alt_ft < low_alt and q.speed_kts is not None
             and q.speed_kts < slow for q in edge):
        why = "low-slow"
    if why is None:
        return None
    rate = _vrate(edge)
    climb = float(ac.get("climb_fpm", 300))
    if rate is not None and rate < -climb and (p.alt_ft or 0) > 1000:
        return None  # the extreme report is HIGHER than its neighbours: not a deck end
    min_km = float(ac.get("open_ocean_km", 80))
    if not geo.open_ocean(p.lat, p.lon, min_km):
        return None
    rotary = t in model.rotary
    if (rotary or (p.speed_kts or 0) < 120) and geo.near_platform(
            p.lat, p.lon, float(ac.get("platform_km", 25))):
        return None
    coast = geo.coast_km(p.lat, p.lon)
    af, _ = geo.airports.nearest_km(p.lat, p.lon, 600.0)
    w = mult * float((ac.get("signature_factor") or {}).get(role, 1.0))
    reason = ""
    if rate is not None and abs(rate) >= climb:
        reason = f"; {'climbing' if role == 'launch' else 'descending'} ~{abs(rate):.0f} ft/min"
    if t in model.fighters:
        _, o_id = geo.airports.nearest_km(other.lat, other.lon, 15.0)
        if o_id is not None and coast < float(ac.get("training_coast_km", 200)):
            w *= float(ac.get("training_factor", 0.35))
            reason += f"; other end at airfield {o_id} - possible training-range sortie"
    return AirFix(track_id=leg.track_id, ts=p.ts, lat=p.lat, lon=p.lon, role=role, type_code=t,
                  callsign=(meta.get("name") or "").strip(), label=label, weight=w,
                  alt_ft=p.alt_ft, speed_kts=p.speed_kts, airfield_km=af, coast_km=coast,
                  reason=reason, sigma_km=20.0 if rotary or role == "recovery" else 30.0,
                  path=_thin(leg.points))


def _loiter(leg: Leg, mult: float, label: str, t: str, meta: dict, geo: Geo, ac: dict
            ) -> list[AirFix]:
    lc = ac.get("loiter") or {}
    min_km = float(ac.get("open_ocean_km", 80))
    max_alt = float(lc.get("max_alt_ft", 3000))
    runs: list[list[Pt]] = [[]]
    for p in leg.points:
        ok = (p.alt_ft is not None and p.alt_ft <= max_alt
              and geo.coast_km(p.lat, p.lon) > min_km)
        if ok:
            runs[-1].append(p)
        elif runs[-1]:
            runs.append([])
    out = []
    for run in runs:
        if len(run) < 4:
            continue
        dur_min = (run[-1].ts - run[0].ts).total_seconds() / 60.0
        if dur_min < float(lc.get("min_minutes", 30)):
            continue
        lat, lon = mean_point([(p.lat, p.lon) for p in run])
        if max(haversine_km(lat, lon, p.lat, p.lon) for p in run) > float(lc.get("max_radius_km", 45)):
            continue
        half = len(run) // 2
        a = mean_point([(p.lat, p.lon) for p in run[:half]])
        b = mean_point([(p.lat, p.lon) for p in run[half:]])
        dt_h = ((run[half:][len(run[half:]) // 2].ts - run[:half][half // 2].ts).total_seconds()
                / 3600.0)
        drift = haversine_km(*a, *b) / 1.852 / dt_h if dt_h > 0 else 0.0
        if drift > float(lc.get("max_drift_kts", 35)):
            continue
        if not geo.open_ocean(lat, lon, min_km) or geo.near_platform(
                lat, lon, float(ac.get("platform_km", 25))):
            continue
        last = run[-1]
        af, _ = geo.airports.nearest_km(lat, lon, 600.0)
        out.append(AirFix(
            track_id=leg.track_id, ts=last.ts, lat=lat, lon=lon, role="loiter", type_code=t,
            callsign=(meta.get("name") or "").strip(), label=label,
            weight=mult * float((ac.get("signature_factor") or {}).get("loiter", 0.8)),
            alt_ft=last.alt_ft, speed_kts=last.speed_kts, airfield_km=af,
            coast_km=geo.coast_km(lat, lon),
            reason=(f"; {dur_min:.0f} min within {max(haversine_km(lat, lon, p.lat, p.lon) for p in run):.0f} km "
                    f"below {max_alt:.0f} ft, centre drifting ~{drift:.0f} kt"),
            sigma_km=15.0, path=_thin(run)))
    return out


def track_fixes(legs: list[Leg], meta: dict | None, geo: Geo, cfg, model: AircraftModel
                ) -> list[AirFix]:
    """Launch / recovery / loiter fixes of one aircraft's legs."""
    meta = meta or {}
    tid = legs[0].track_id if legs else ""
    mult, label, t = model.classify(tid, meta)
    if mult <= 0:
        return []
    ac = cfg.sec("aircraft")
    k = int(ac.get("edge_points", 4))
    max_kts = float(ac.get("max_speed_kts", 650))
    out: list[AirFix] = []
    for leg in legs:
        pts = _clean(leg.points, max_kts)
        if len(pts) < 2:
            continue
        lg = Leg(leg.track_id, pts, leg.starts_in_window, leg.ended)
        if lg.starts_in_window:
            f = _edge_fix(lg, pts[:k], "launch", pts[-1], mult, label, t, meta, geo, ac, model)
            if f:
                out.append(f)
        if lg.ended:
            f = _edge_fix(lg, pts[::-1][:k], "recovery", pts[0], mult, label, t, meta, geo, ac,
                          model)
            if f:
                out.append(f)
        if t in model.rotary:
            out += _loiter(lg, mult, label, t, meta, geo, ac)
    return out
