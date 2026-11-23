"""Flight-leg segmentation and the per-track air indicators (sea-terminating legs,
carrier-air-wing types over ocean, over-ocean orbits)."""

import itertools
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple

from osint.core.geo import haversine_km
from osint.naval.geo import Geo, angle_diff, bearing_deg, mean_point
from osint.naval.models import Evidence

# land-based maritime patrol / ISR types that routinely fly low over the sea and drop out of
# ADS-B coverage: never counted as sea-terminating
DEFAULT_EXCLUDE = {"P8", "P3", "ATLA", "C295", "CN35", "Q9", "Q4", "MQ4C", "K35R", "KC46", "E3CF",
                   "E3TF", "R135", "E6", "C17", "C5M", "A400"}


class Pt(NamedTuple):
    ts: datetime
    lat: float
    lon: float
    alt_ft: int | None
    speed_kts: float | None
    heading_deg: float | None


@dataclass
class Leg:
    track_id: str
    points: list[Pt]
    starts_in_window: bool   # not a continuation of data before the window
    ended: bool              # the aircraft disappeared (not still live)

    @property
    def first(self) -> Pt:
        return self.points[0]

    @property
    def last(self) -> Pt:
        return self.points[-1]


def segment_legs(track_id: str, pts: list[Pt], *, window_start: datetime, now: datetime,
                 max_gap: timedelta, live_grace: timedelta) -> list[Leg]:
    pts = sorted(pts, key=lambda p: p.ts)
    if not pts:
        return []
    groups: list[list[Pt]] = [[pts[0]]]
    for p in pts[1:]:
        if p.ts - groups[-1][-1].ts > max_gap:
            groups.append([p])
        else:
            groups[-1].append(p)
    legs = []
    for i, g in enumerate(groups):
        starts = i > 0 or g[0].ts - window_start > max_gap
        ended = i < len(groups) - 1 or now - g[-1].ts > live_grace
        legs.append(Leg(track_id, g, starts, ended))
    return legs


def type_multiplier(type_code: str | None, table: dict) -> float:
    return float(table.get((type_code or "").upper().strip(), 0.0))


def _low(pts: list[Pt], low_alt: float, slow: float, appear_alt: float) -> str | None:
    """Why this end of a leg is 'at low level' (or None)."""
    edge = pts[0]
    if edge.alt_ft is not None and edge.alt_ft <= appear_alt:
        return f"at {edge.alt_ft} ft"
    for p in pts:
        if (p.alt_ft is not None and p.alt_ft < low_alt and p.speed_kts is not None
                and p.speed_kts < slow):
            return f"descending to {p.alt_ft} ft at {p.speed_kts:.0f} kt"
    return None


def sea_terminating(legs: list[Leg], meta: dict | None, geo: Geo, cfg: dict,
                    carrier_types: dict) -> list[Evidence]:
    """Legs that start or end at low level over open ocean far from coast and airfields."""
    meta = meta or {}
    t = (meta.get("type_code") or "").upper()
    if t in set(cfg.get("exclude_types") or DEFAULT_EXCLUDE):
        return []
    mult = type_multiplier(t, carrier_types)
    low_alt = float(cfg.get("low_alt_ft", 1500))
    slow = float(cfg.get("slow_kts", 200))
    appear = float(cfg.get("appear_alt_ft", 3000))
    min_km = float(cfg.get("open_ocean_km", 80))
    base = float(cfg.get("base_weight", 1.0))
    if mult <= 0:  # generic military type: must be both low and slow, reduced weight
        appear = min(appear, low_alt)
        weight = base * float(cfg.get("generic_weight_factor", 0.5))
        if weight <= 0:
            return []
    else:
        weight = base * mult
    out: list[Evidence] = []
    label = meta.get("name") or meta.get("reg") or ""
    for leg in legs:
        if len(leg.points) < int(cfg.get("min_points", 2)):
            continue
        ends = []
        if leg.starts_in_window:
            ends.append(("start", leg.points[:3]))
        if leg.ended:
            ends.append(("end", leg.points[::-1][:3]))
        for which, edge in ends:
            p = edge[0]
            why = _low(edge, low_alt, slow, appear)
            if why is None or not geo.open_ocean(p.lat, p.lon, min_km):
                continue
            if geo.near_platform(p.lat, p.lon, float(cfg.get("platform_km", 25))):
                continue  # offshore oil/gas platform (helideck) proxy
            coast = geo.coast_km(p.lat, p.lon)
            verb = "appeared" if which == "start" else "disappeared"
            out.append(Evidence(
                kind="sea_terminating_flight", ts=p.ts, lat=p.lat, lon=p.lon, weight=weight,
                description=(f"{t or 'military aircraft'} {label} {verb} {why} over open ocean "
                             f"~{coast:.0f} km from land/airfields").replace("  ", " "),
                refs=[{"type": "track", "id": leg.track_id, "at": p.ts.isoformat()}],
                source_key=leg.track_id))
    return out


def cvw_over_ocean(track_id: str, pts: list[Pt], meta: dict | None, geo: Geo, cfg: dict,
                   carrier_types: dict) -> list[Evidence]:
    """Carrier-air-wing types airborne far from land (one sample per track per interval)."""
    meta = meta or {}
    t = (meta.get("type_code") or "").upper()
    mult = type_multiplier(t, carrier_types)
    if mult <= 0:
        return []
    far = float(cfg.get("far_from_land_km", 150))
    step = timedelta(minutes=float(cfg.get("sample_min", 30)))
    w = float(cfg.get("cvw_weight", 0.6)) * mult
    out: list[Evidence] = []
    last: datetime | None = None
    for p in sorted(pts, key=lambda x: x.ts):
        if last is not None and p.ts - last < step:
            continue
        if p.alt_ft == 0:
            continue
        coast = geo.coast_km(p.lat, p.lon)
        if coast <= far:
            continue
        last = p.ts
        out.append(Evidence(
            kind="cvw_aircraft", ts=p.ts, lat=p.lat, lon=p.lon, weight=w,
            description=(f"carrier-air-wing type {t} {meta.get('name') or ''} airborne "
                         f"~{coast:.0f} km from land").replace("  ", " "),
            refs=[{"type": "track", "id": track_id, "at": p.ts.isoformat()}],
            source_key=track_id))
    return out


def orbit(leg: Leg, meta: dict | None, geo: Geo, cfg: dict, far_km: float) -> Evidence | None:
    """Racetrack/orbit far from land: cumulative turning >= min_turn_deg inside a small area."""
    meta = meta or {}
    far = [p for p in leg.points if geo.coast_km(p.lat, p.lon) > far_km]
    if len(far) < 6:
        return None
    dur = far[-1].ts - far[0].ts
    if dur < timedelta(minutes=float(cfg.get("min_duration_min", 30))):
        return None
    lat, lon = mean_point([(p.lat, p.lon) for p in far])
    if max(haversine_km(lat, lon, p.lat, p.lon) for p in far) > float(cfg.get("max_radius_km", 120)):
        return None
    turn = 0.0
    prev = None
    for a, b in itertools.pairwise(far):
        if haversine_km(a.lat, a.lon, b.lat, b.lon) < 0.5:
            continue
        brg = bearing_deg(a.lat, a.lon, b.lat, b.lon)
        if prev is not None:
            turn += abs(angle_diff(prev, brg))
        prev = brg
    if turn < float(cfg.get("min_turn_deg", 540)):
        return None
    t = (meta.get("type_code") or "").upper()
    w = float((cfg.get("types") or {}).get(t, cfg.get("default_weight", 0.6)))
    return Evidence(
        kind="orbit", ts=far[-1].ts, lat=lat, lon=lon, weight=w,
        description=(f"{t or 'military aircraft'} {meta.get('name') or ''} orbited "
                     f"{turn:.0f} deg over {dur.total_seconds() / 60:.0f} min far from land"
                     ).replace("  ", " "),
        refs=[{"type": "track", "id": leg.track_id, "at": far[-1].ts.isoformat()}],
        radius_km=float(cfg.get("max_radius_km", 120)) / 2, source_key=leg.track_id)
