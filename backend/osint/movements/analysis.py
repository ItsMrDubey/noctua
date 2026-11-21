"""Corridor aggregation, surge detection and naval selection (pure functions; no I/O)."""

import math
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import h3

from osint.movements.config import CorridorsConfig, SurgesConfig

US_PER_H = 3_600_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _iso(us: int) -> str:
    return (_EPOCH + timedelta(microseconds=us)).isoformat()


# ---------------------------------------------------------------- corridors

def great_circle(lat1: float, lon1: float, lat2: float, lon2: float, n: int = 32) -> list[list[float]]:
    """``n`` points [lon, lat] along the great circle (slerp on the unit sphere)."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = (math.cos(p1) * math.cos(l1), math.cos(p1) * math.sin(l1), math.sin(p1))
    b = (math.cos(p2) * math.cos(l2), math.cos(p2) * math.sin(l2), math.sin(p2))
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b, strict=True))))
    om = math.acos(dot)
    out = []
    for i in range(n):
        t = i / (n - 1)
        if om < 1e-9:
            v = a
        else:
            s1, s2 = math.sin((1 - t) * om) / math.sin(om), math.sin(t * om) / math.sin(om)
            v = tuple(s1 * x + s2 * y for x, y in zip(a, b, strict=True))
        lat = math.degrees(math.atan2(v[2], math.hypot(v[0], v[1])))
        lon = math.degrees(math.atan2(v[1], v[0]))
        out.append([round(lon, 4), round(lat, 4)])
    return out


def corridors(legs: list[dict], cfg: CorridorsConfig, role: str | None = None) -> list[dict]:
    """(origin, dest, role) -> count, last_seen, callsigns, great-circle polyline. Only legs
    with two known, different endpoints form a corridor."""
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for lg in legs:
        o, d = lg.get("origin"), lg.get("dest")
        if not o or not d or o["ident"] == d["ident"] or (role and lg["role"] != role):
            continue
        groups[(o["ident"], d["ident"], lg["role"])].append(lg)
    out = []
    for (oi, di, r), items in groups.items():
        items.sort(key=lambda x: x["t_end_us"], reverse=True)
        o, d = items[0]["origin"], items[0]["dest"]
        calls: list[str] = []
        for x in items:
            c = x.get("callsign") or x["track_id"]
            if c not in calls:
                calls.append(c)
        out.append({
            "id": f"{oi}>{di}:{r}", "origin": o, "dest": d, "role": r, "count": len(items),
            "aircraft": len({x["track_id"] for x in items}), "last_seen": items[0]["t_end"],
            "callsigns": calls[: cfg.max_callsigns],
            "types": sorted({x["type_code"] for x in items if x.get("type_code")}),
            "path": great_circle(o["lat"], o["lon"], d["lat"], d["lon"], cfg.arc_points),
        })
    out.sort(key=lambda c: (-c["count"], c["id"]))
    return out


# ------------------------------------------------------------------- surges

def cell_counts(rows, res: int, bucket_us: int) -> dict[tuple[str, int], set[str]]:
    """rows: (track_id, ts_us, lat, lon) -> {(cell, bucket index): {track ids}}."""
    out: dict[tuple[str, int], set[str]] = defaultdict(set)
    for tid, ts_us, lat, lon in rows:
        out[(h3.latlng_to_cell(lat, lon, res), ts_us // bucket_us)].add(tid)
    return out


def surges(counts: dict[tuple[str, int], set[str]], now_us: int, hours: int, cfg: SurgesConfig,
           history_start_us: int | None, names: dict[str, str] | None = None) -> dict:
    """Flag (cell, bucket) pairs in the last ``hours`` whose distinct-aircraft count is
    >= min_count and z >= z_min against the same cell and bucket-of-day over the previous
    baseline_days (missing buckets count as 0)."""
    bucket_us = cfg.bucket_hours * US_PER_H
    per_day = max(1, 24 // cfg.bucket_hours)
    cur_b = now_us // bucket_us
    first_b = (now_us - hours * US_PER_H) // bucket_us
    days_of_history = 0.0 if history_start_us is None else (now_us - history_start_us) / (24 * US_PER_H)
    enough = days_of_history >= cfg.min_baseline_days
    flagged = []
    for (cell, b), tracks in counts.items():
        if b < first_b or b > cur_b or len(tracks) < cfg.min_count:
            continue
        base = []
        for k in range(1, cfg.baseline_days + 1):
            bb = b - k * per_day
            if history_start_us is not None and (bb + 1) * bucket_us <= history_start_us:
                continue  # no data collected yet for that bucket: not a zero
            base.append(len(counts.get((cell, bb), ())))
        mean = sum(base) / len(base) if base else 0.0
        var = sum((x - mean) ** 2 for x in base) / len(base) if base else 0.0
        std = max(math.sqrt(var), cfg.std_floor)
        z = (len(tracks) - mean) / std
        if not enough or len(base) < cfg.min_baseline_days or z < cfg.z_min:
            continue
        lat, lon = h3.cell_to_latlng(cell)
        flagged.append({
            "id": f"{cell}:{b}", "cell": cell, "lat": round(lat, 4), "lon": round(lon, 4),
            "boundary": [[round(x[1], 4), round(x[0], 4)] for x in h3.cell_to_boundary(cell)],
            "bucket_start": _iso(b * bucket_us), "bucket_hours": cfg.bucket_hours, "current": len(tracks),
            "baseline_mean": round(mean, 2), "baseline_std": round(math.sqrt(var), 2),
            "baseline_n": len(base), "z": round(z, 2),
            "ratio": round(len(tracks) / mean, 2) if mean > 0 else None,
            "callsigns": sorted({(names or {}).get(t) or t for t in tracks})[:8],
        })
    flagged.sort(key=lambda x: (-x["z"], x["id"]))
    return {"items": flagged, "baseline_insufficient": not enough,
            "history_days": round(days_of_history, 2)}


# -------------------------------------------------------------------- naval

def naval_regex(prefixes: list[str]) -> re.Pattern:
    return re.compile(r"^\s*(?:" + "|".join(re.escape(p) for p in prefixes) + r")[\s.]+\S")


def is_naval(meta: dict | None, rx: re.Pattern) -> bool:
    meta = meta or {}
    if meta.get("is_mil"):
        return True
    return bool(rx.match(meta.get("name") or ""))
