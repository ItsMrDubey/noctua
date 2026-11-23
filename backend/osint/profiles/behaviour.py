"""What an asset has been doing, from our own data only: the lake track (positions), the live
emergency-squawk and GNSS snapshots (``data/signals``), watchlist / infrastructure alerts
(``infra_watch.sqlite``), news / sensor events (``osint.sqlite``) and the front line
(``deepstate_frontline.json``).

The analysis functions are pure (points in, facts out) so they are testable without a lake;
``gather`` does the I/O. Every fact states its window and source; nothing is inferred beyond
the geometry (an "orbit" is a long path inside a small radius, a "gap" is a time without
fixes in our feed: reception gaps and AIS switched off look the same).
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from osint.core.geo import haversine_km, utcnow
from osint.fires.geo import PolygonIndex, SegmentIndex

log = logging.getLogger(__name__)

SPARK_N = 48


def _ts(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(v))


def _iso(d: datetime) -> str:
    return d.astimezone(UTC).isoformat(timespec="seconds")


def downsample(vals: list, n: int = SPARK_N) -> list:
    if len(vals) <= n:
        return list(vals)
    return [vals[round(i * (len(vals) - 1) / (n - 1))] for i in range(n)]


def summarize(points: list[dict]) -> dict | None:
    """Fix count, time span, distance, altitude / speed ranges and sparkline series."""
    if not points:
        return None
    dist = sum(haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
               for a, b in itertools.pairwise(points))
    alts = [p["alt_ft"] for p in points if p.get("alt_ft") is not None]
    spd = [p["speed_kts"] for p in points if p.get("speed_kts") is not None]
    lats, lons = [p["lat"] for p in points], [p["lon"] for p in points]
    return {"fixes": len(points), "first": points[0]["ts"], "last": points[-1]["ts"],
            "distance_km": round(dist), "max_alt_ft": max(alts) if alts else None,
            "max_speed_kts": round(max(spd), 1) if spd else None,
            "mean_speed_kts": round(sum(spd) / len(spd), 1) if spd else None,
            "bbox": [round(min(lons), 3), round(min(lats), 3), round(max(lons), 3),
                     round(max(lats), 3)],
            "spark": {"alt_ft": downsample([p.get("alt_ft") for p in points]),
                      "speed_kts": downsample([p.get("speed_kts") for p in points])}}


def orbit(points: list[dict], min_path_km: float = 300.0, ratio: float = 4.0) -> dict | None:
    """A long path inside a small circle (tanker track, ISR racetrack, patrol box):
    path length >= ``ratio`` x the max distance from the centroid, and >= ``min_path_km``."""
    if len(points) < 10:
        return None
    lat0 = sum(p["lat"] for p in points) / len(points)
    lon0 = sum(p["lon"] for p in points) / len(points)
    radius = max(haversine_km(lat0, lon0, p["lat"], p["lon"]) for p in points)
    path = sum(haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
               for a, b in itertools.pairwise(points))
    if path < min_path_km or path < ratio * max(radius, 1.0):
        return None
    alts = sorted(p["alt_ft"] for p in points if p.get("alt_ft"))
    return {"centre": [round(lat0, 3), round(lon0, 3)], "radius_km": round(radius),
            "path_km": round(path), "laps": round(path / (2 * math.pi * max(radius, 1.0)), 1),
            "median_alt_ft": alts[len(alts) // 2] if alts else None,
            "from": points[0]["ts"], "to": points[-1]["ts"]}


def gaps(points: list[dict], min_h: float) -> list[dict]:
    """Periods of >= ``min_h`` hours without a fix, with the jump across each gap."""
    out = []
    for a, b in itertools.pairwise(points):
        h = (_ts(b["ts"]) - _ts(a["ts"])).total_seconds() / 3600
        if h >= min_h:
            km = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
            out.append({"from": a["ts"], "to": b["ts"], "hours": round(h, 1),
                        "jump_km": round(km, 1), "implied_kts": round(km / 1.852 / h, 1),
                        "lat": a["lat"], "lon": a["lon"], "lat2": b["lat"], "lon2": b["lon"]})
    return out


def jumps(points: list[dict], max_kts: float, min_km: float = 5.0) -> list[dict]:
    """Consecutive fixes implying an impossible speed (position spoofing / a bad fix)."""
    out = []
    for a, b in itertools.pairwise(points):
        h = (_ts(b["ts"]) - _ts(a["ts"])).total_seconds() / 3600
        km = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
        if km >= min_km and h > 0 and km / 1.852 / h > max_kts:
            out.append({"at": b["ts"], "km": round(km, 1), "implied_kts": round(km / 1.852 / h),
                        "lat": b["lat"], "lon": b["lon"]})
    return out


def stops(points: list[dict], ports: list[dict], max_kts: float = 1.0, min_h: float = 2.0,
          port_km: float = 15.0) -> list[dict]:
    """Stationary periods (speed <= ``max_kts`` for >= ``min_h`` hours), each with the nearest
    known port within ``port_km``."""
    out, run = [], []
    for p in points + [None]:
        slow = p is not None and p.get("speed_kts") is not None and p["speed_kts"] <= max_kts
        if slow:
            run.append(p)
            continue
        if len(run) >= 2 and (_ts(run[-1]["ts"]) - _ts(run[0]["ts"])).total_seconds() >= min_h * 3600:
            lat = sum(x["lat"] for x in run) / len(run)
            lon = sum(x["lon"] for x in run) / len(run)
            best = None
            for port in ports:
                if abs(port["lat"] - lat) > 0.3:
                    continue
                d = haversine_km(lat, lon, port["lat"], port["lon"])
                if d <= port_km and (best is None or d < best[0]):
                    best = (d, port)
            out.append({"from": run[0]["ts"], "to": run[-1]["ts"], "lat": round(lat, 4),
                        "lon": round(lon, 4), "port": best[1]["name"] if best else None,
                        "port_country": (best[1].get("country") or None) if best else None,
                        "port_km": round(best[0], 1) if best else None})
        run = []
    return out


class CountryIndex:
    """Point -> country (ISO-2 + name) over the Natural Earth 1:50m outlines."""

    def __init__(self, countries) -> None:
        self.items = []
        for cc, polys in countries.shapes.items():
            rings = [[[list(pt) for pt in zip(r[::2], r[1::2], strict=False)] for r in poly]
                     for poly in polys]
            xs = [pt[0] for poly in rings for pt in poly[0]] if rings else []
            ys = [pt[1] for poly in rings for pt in poly[0]] if rings else []
            if not xs:
                continue
            name = countries.points.get(cc, (0, 0, cc))[2]
            self.items.append((cc, name, (min(xs), min(ys), max(xs), max(ys)),
                               PolygonIndex(rings, band_deg=0.25)))

    def at(self, lat: float, lon: float) -> tuple[str, str] | None:
        for cc, name, (x0, y0, x1, y1), idx in self.items:
            if x0 <= lon <= x1 and y0 <= lat <= y1 and idx.contains(lat, lon):
                return cc, name
        return None


_ci_lock = threading.Lock()
_ci: CountryIndex | None = None


def country_index() -> CountryIndex | None:
    global _ci
    with _ci_lock:
        if _ci is None:
            try:
                from osint.signals.countries import get_countries

                _ci = CountryIndex(get_countries())
            except Exception as exc:  # noqa: BLE001 — optional context
                log.warning("country index unavailable: %s", exc)
                return None
        return _ci


def countries_crossed(points: list[dict], index: CountryIndex | None, n: int = 96) -> list[dict]:
    """Countries under the (downsampled) track in order of first appearance; sea fixes are
    counted as ``over_sea``."""
    if index is None or not points:
        return []
    out: dict[str, dict] = {}
    sea = 0
    for p in downsample(points, n):
        hit = index.at(p["lat"], p["lon"])
        if hit is None:
            sea += 1
            continue
        cc, name = hit
        row = out.setdefault(cc, {"cc": cc, "name": name, "first": p["ts"], "samples": 0})
        row["samples"] += 1
    res = list(out.values())
    if sea:
        res.append({"cc": None, "name": "Over sea / international", "samples": sea})
    return res


def frontline_distance(points: list[dict], lines: list[list[tuple[float, float]]],
                       max_km: float = 150.0) -> dict | None:
    if not lines or not points:
        return None
    idx = SegmentIndex(lines, cell_deg=0.5)
    best = None
    for p in downsample(points, 200):
        d = idx.distance(p["lat"], p["lon"], max_km)
        if d is not None and (best is None or d < best["km"]):
            best = {"km": round(d, 1), "at": p["ts"], "lat": p["lat"], "lon": p["lon"]}
    return best


def events_near(points: list[dict], events: list[dict], km: float, slack_h: float = 2.0,
                limit: int = 6) -> list[dict]:
    """Events within ``km`` of the track at a time the asset was there (event window +-
    ``slack_h``), closest first."""
    if not points or not events:
        return []
    pts = downsample(points, 300)
    tp = [(_ts(p["ts"]), p) for p in pts]
    out = []
    for ev in events:
        if ev.get("lat") is None:
            continue
        t0 = _ts(ev.get("t_start") or ev.get("updated_at") or points[-1]["ts"])
        t1 = _ts(ev.get("t_end") or ev.get("t_start") or ev.get("updated_at") or t0)
        lo, hi = t0 - timedelta(hours=slack_h), t1 + timedelta(hours=slack_h)
        best = None
        for t, p in tp:
            if lo <= t <= hi:
                d = haversine_km(ev["lat"], ev["lon"], p["lat"], p["lon"])
                if d <= km and (best is None or d < best[0]):
                    best = (d, p["ts"])
        if best:
            out.append({"id": ev["id"], "title": ev.get("title_en") or ev.get("title"),
                        "category": ev.get("category"), "status": ev.get("status"),
                        "km": round(best[0], 1), "at": best[1], "lat": ev["lat"],
                        "lon": ev["lon"], "t_start": ev.get("t_start")})
    out.sort(key=lambda x: x["km"])
    return out[:limit]


def gnss_hits(points: list[dict], snapshot: dict | None, window_min: float | None = None) -> list[dict]:
    """GNSS-interference cells (medium / high) of the live snapshot that the recent track
    crossed (only fixes inside the snapshot window count)."""
    cells = {c["h3"]: c for c in (snapshot or {}).get("cells") or []
             if c.get("level") in ("medium", "high")}
    if not cells or not points:
        return []
    import h3

    res = int((snapshot or {}).get("res") or 3)
    win = float(window_min or (snapshot or {}).get("window_min") or 60)
    upd = (snapshot or {}).get("updated")
    since = (datetime.fromtimestamp(upd, UTC) if upd else utcnow()) - timedelta(minutes=win)
    out: dict[str, dict] = {}
    for p in points:
        if _ts(p["ts"]) < since:
            continue
        c = cells.get(h3.latlng_to_cell(p["lat"], p["lon"], res))
        if c and c["h3"] not in out:
            out[c["h3"]] = {"cell": c["h3"], "level": c["level"], "pct": c.get("pct"),
                            "aircraft": c.get("n"), "degraded": c.get("bad"), "at": p["ts"],
                            "lat": c.get("lat"), "lon": c.get("lon")}
    return list(out.values())


def squawk_history(snapshot: dict | None, hex_code: str) -> list[dict]:
    out = []
    for e in (snapshot or {}).get("items") or []:
        if str(e.get("hex") or "").lower() == hex_code:
            out.append({k: e.get(k) for k in ("code", "label", "flight", "active", "alt_ft",
                                              "lat", "lon", "source")}
                       | {"first_seen": _iso(datetime.fromtimestamp(e["first_seen"], UTC))
                          if e.get("first_seen") else None,
                          "last_seen": _iso(datetime.fromtimestamp(e["last_seen"], UTC))
                          if e.get("last_seen") else None})
    return out


# ------------------------------------------------------------------ I/O


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def watch_alerts(path: Path, track_id: str, limit: int = 20) -> list[dict]:
    """Watchlist / infrastructure-proximity alerts of one track (read-only)."""
    if not path.is_file():
        return []
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = db.execute("SELECT id, ts, kind, severity, title, sub, lat, lon, detail FROM "
                              "alerts WHERE track_id=? ORDER BY id DESC LIMIT ?",
                              (track_id, limit)).fetchall()
        finally:
            db.close()
    except sqlite3.Error as exc:
        log.info("watch alerts unreadable: %s", exc)
        return []
    out = []
    for r in rows:
        try:
            detail = json.loads(r[8] or "{}")
        except ValueError:
            detail = {}
        out.append({"id": r[0], "ts": r[1], "kind": r[2], "severity": r[3], "title": r[4],
                    "sub": r[5], "lat": r[6], "lon": r[7], "detail": detail})
    return out


def lake_points(runtime, track_id: str, hours: float) -> list[dict]:
    from osint.api.history import track_points

    try:
        return track_points(runtime, track_id, math.ceil(hours))
    except Exception as exc:  # noqa: BLE001 — the lake is optional context
        log.info("lake track %s unavailable: %s", track_id, exc)
        return []


def lake_coverage(runtime) -> str | None:
    """Date of the oldest positions partition (how far back "seen on N days" can look)."""
    root = getattr(getattr(runtime, "lake", None), "root", None)
    if root is None:
        return None
    dates = sorted(p.name.removeprefix("date=") for p in (Path(root) / "positions").glob("date=*"))
    return dates[0] if dates else None


def days_seen(runtime, track_id: str, days: int = 30) -> dict | None:
    since = utcnow() - timedelta(days=days)
    try:
        rows = runtime.lake.query(
            "SELECT count(DISTINCT CAST(ts AS DATE)), min(ts), max(ts), count(*) FROM positions "
            "WHERE track_id = ? AND ts >= ?", [track_id, since], start=since)
    except Exception as exc:  # noqa: BLE001
        log.info("days seen %s unavailable: %s", track_id, exc)
        return None
    n, first, last, fixes = rows[0] if rows else (0, None, None, 0)
    return {"days": int(n or 0), "window_days": days, "fixes": int(fixes or 0),
            "first": _iso(first) if first else None, "last": _iso(last) if last else None,
            "coverage_since": lake_coverage(runtime)}


def events_around(runtime, points: list[dict], km: float, hours: float,
                  exclude: tuple[str, ...] = ()) -> list[dict]:
    """Events near the track, minus the asset's own derived events (e.g. a "military orbit"
    sensor event naming this callsign / registration)."""
    if not points:
        return []
    lats = [p["lat"] for p in points]
    lons = [p["lon"] for p in points]
    pad = km / 111.0 + 0.1
    bbox = (min(lons) - pad * 2, min(lats) - pad, max(lons) + pad * 2, max(lats) + pad)
    try:
        evs = runtime.store.list_events(bbox=bbox, since=utcnow() - timedelta(hours=hours + 24),
                                        include_unlocated=False, map_default=True, limit=800)
    except Exception as exc:  # noqa: BLE001
        log.info("events lookup failed: %s", exc)
        return []
    words = [w.upper() for w in exclude if w and len(w) >= 3]
    evs = [e for e in evs if not any(w in (e.get("title") or "").upper() for w in words)]
    return events_near(points, evs, km)
