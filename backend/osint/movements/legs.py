"""Segment a military aircraft's position history into flight legs.

A leg ends when the aircraft is on / near the ground (alt <= ground_alt_ft and speed <
ground_speed_kts) within airport_radius_km of a reference airport, or when there is a gap
longer than gap_min. Endpoints resolve to the nearest reference airport; when the aircraft
was not seen on the ground, a first / last fix below low_alt_ft near an airport is accepted
as the departure / arrival. Anything else stays unknown.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from osint.movements.airports import AirportIndex, airport_ref
from osint.movements.config import LegsConfig

US_PER_MIN = 60_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(slots=True)
class Fix:
    ts_us: int
    lat: float
    lon: float
    alt_ft: float | None
    speed_kts: float | None


def is_ground(f: Fix, cfg: LegsConfig) -> bool:
    if f.speed_kts is None or f.speed_kts >= cfg.ground_speed_kts:
        return False
    return f.alt_ft is None or f.alt_ft <= cfg.ground_alt_ft


def _iso(us: int) -> str:
    return (_EPOCH + timedelta(microseconds=us)).isoformat()


def downsample(fixes: list[Fix], n: int) -> list[list[float]]:
    if len(fixes) <= n:
        pick = fixes
    else:
        step = (len(fixes) - 1) / (n - 1)
        pick = [fixes[round(i * step)] for i in range(n)]
    return [[round(f.lon, 4), round(f.lat, 4)] for f in pick]


def segment(track_id: str, fixes: list[Fix], index: AirportIndex, cfg: LegsConfig,
            now_us: int, meta: dict | None = None, role: str = "other") -> list[dict]:
    """Legs of one track; ``fixes`` sorted by time."""
    meta = meta or {}
    gap_us = cfg.gap_min * US_PER_MIN
    legs: list[dict] = []
    cur: list[Fix] = []
    origin = None            # (airport, how) for the open leg
    last_ground = None       # (airport, ts_us) of the latest ground fix
    prev: Fix | None = None

    def near(f: Fix):
        hit = index.nearest(f.lat, f.lon, cfg.airport_radius_km)
        return hit[0] if hit else None

    def close(dest, how: str, open_end: bool) -> None:
        nonlocal cur, origin
        if len(cur) >= cfg.min_points:
            last = cur[-1]
            if dest is None and not open_end and last.alt_ft is not None and last.alt_ft <= cfg.low_alt_ft:
                dest, how = near(last), "approach"
                if dest is None:
                    how = "unknown"
            status = "landed" if dest is not None else (
                "in_flight" if open_end and now_us - last.ts_us <= cfg.in_flight_min * US_PER_MIN else "unknown")
            o_ap, o_how = origin if origin else (None, "unknown")
            alts = [f.alt_ft for f in cur if f.alt_ft is not None]
            legs.append({
                "id": f"{track_id}@{cur[0].ts_us // 1_000_000}", "track_id": track_id,
                "callsign": meta.get("name"), "type_code": meta.get("type_code"),
                "operator": meta.get("operator"), "role": role,
                "origin": airport_ref(o_ap), "origin_how": o_how if o_ap else "unknown",
                "dest": airport_ref(dest), "dest_how": how if dest is not None else "unknown",
                "status": status, "t_start": _iso(cur[0].ts_us), "t_end": _iso(last.ts_us),
                "t_start_us": cur[0].ts_us, "t_end_us": last.ts_us, "n_fixes": len(cur),
                "max_alt_ft": max(alts) if alts else None,
                "path": downsample(cur, cfg.max_path_points),
            })
        cur, origin = [], None

    for f in fixes:
        if prev is not None and f.ts_us - prev.ts_us > gap_us and cur:
            close(None, "unknown", False)
        prev = f
        if is_ground(f, cfg):
            ap = near(f)
            if ap is None:
                continue  # slow + low far from any airport: ignore (bad fix / hover)
            if cur:
                close(ap, "ground", False)
            last_ground = (ap, f.ts_us)
            continue
        if not cur:
            if last_ground and f.ts_us - last_ground[1] <= gap_us:
                origin = (last_ground[0], "ground")
            elif f.alt_ft is not None and f.alt_ft <= cfg.low_alt_ft:
                ap = near(f)
                origin = (ap, "climb") if ap else None
            last_ground = None
        cur.append(f)
    if cur:
        close(None, "unknown", True)
    return legs
