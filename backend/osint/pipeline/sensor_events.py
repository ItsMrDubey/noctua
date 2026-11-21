"""Sensor-derived events (spec §1 "domain depth", §5 stage 6): patterns in the sensor data
that are events by themselves, with no news report needed.

Detectors (thresholds in ``config/corroboration.yaml`` -> ``sensor_events``):

* ``orbit`` — a military tanker / ISR aircraft (type list, or category ISR) flying a racetrack
  or loiter: within a sliding ``window_min`` (60 min) its cumulative absolute heading change
  is >= ``min_turn_deg`` (540 deg, 1.5 full turns) while every position stays within
  ``max_radius_km`` (100 km) of the window centroid. Headings are the reported track, or the
  bearing between fixes; changes across gaps > ``max_gap_min`` are not counted. Straight or
  gently curving flights accumulate far less than 540 deg in an hour.
* ``concentration`` — >= ``min_aircraft`` distinct airborne (>= ``min_alt_ft``) military
  aircraft within one H3 res-3 cell plus its neighbours in the last ``window_min`` (30 min),
  and at least ``ratio`` x the area's baseline + ``min_excess``. Baseline = mean distinct
  airborne aircraft per 30-min bucket over the previous ``baseline_days`` (7) in those cells
  (all air tracks, i.e. conservative: >= the military-only baseline), refreshed hourly.
  Overlapping areas are suppressed in favour of the densest cell.
* ``ais_gap`` — a ship whose consecutive AIS positions are >= ``min_gap_h`` (6 h) apart,
  with an implied speed across the gap <= ``max_drift_kts`` (else a bad fix), while at least
  ``min_coverage_tracks`` other ships kept reporting in the same res-3 area during the gap
  (so the silence is not simply missing receiver coverage).
* ``seismic`` — USGS events typed explosion / chemical / nuclear / mining explosion / quarry
  blast; confidence per type.

Every event gets category air_activity / maritime_security (seismic: configurable), status
``sensor-corroborated`` by definition, ``confidence`` from the pattern strength, and its
supporting tracks / observations as corroboration rows. De-duplication: an event is keyed
``<detector>:<res-3 cell>:<window>``; a detection in the same area (cell or neighbours) whose
time overlaps an existing event of the detector within ``merge_gap_h`` extends that event
instead of creating a new one, so re-running a pass over the same data changes nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

import h3
import yaml

from osint.config import ROOT
from osint.core.geo import haversine_km, utcnow
from osint.core.metrics import Metrics
from osint.pipeline.cluster import CLUSTER_VERSION, new_ulid
from osint.pipeline.corroborate import ts_from_us
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore, iso

if TYPE_CHECKING:
    from osint.runtime import Runtime

log = logging.getLogger(__name__)

DEFAULT_CONFIG = ROOT / "config" / "corroboration.yaml"
DETECTORS = ("orbit", "concentration", "ais_gap", "seismic")
_KTS_KMH = 1.852


@dataclass
class SensorEventsConfig:
    interval_s: float = 300.0
    initial_delay_s: float = 60.0
    orbit: dict[str, Any] = field(default_factory=dict)
    concentration: dict[str, Any] = field(default_factory=dict)
    ais_gap: dict[str, Any] = field(default_factory=dict)
    seismic: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: Path = DEFAULT_CONFIG) -> SensorEventsConfig:
        if not path.is_file():
            return cls()
        with path.open(encoding="utf-8") as f:
            d = (yaml.safe_load(f) or {}).get("sensor_events") or {}
        return cls(interval_s=float(d.get("interval_s", 300)),
                   initial_delay_s=float(d.get("initial_delay_s", 60)),
                   **{k: dict(d.get(k) or {}) for k in DETECTORS})


def _g(d: dict, key: str, default: float) -> float:
    return float(d.get(key, default))


def _angle_diff(a: float, b: float) -> float:
    return (b - a + 180.0) % 360.0 - 180.0


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(x, y)) % 360.0


def _centroid(pts: list[tuple[float, float]]) -> tuple[float, float]:
    lat = sum(p[0] for p in pts) / len(pts)
    # longitudes averaged on the unit circle (antimeridian safe)
    sx = sum(math.cos(math.radians(p[1])) for p in pts)
    sy = sum(math.sin(math.radians(p[1])) for p in pts)
    return lat, math.degrees(math.atan2(sy, sx))


def _cell3(lat: float, lon: float) -> str:
    return h3.latlng_to_cell(lat, lon, 3)


@dataclass
class Detection:
    """One pattern found by a detector (before de-duplication)."""

    detector: str
    category: str
    lat: float
    lon: float
    t_start: datetime
    t_end: datetime
    confidence: float
    title: str
    summary: str
    evidence: list[dict]  # corroboration rows without event_id
    details: dict = field(default_factory=dict)
    merge_gap_h: float = 1.0  # 0 = never merge with an earlier event
    key: str | None = None  # explicit dedupe key (default <detector>:<cell>:<window>)


# ------------------------------------------------------------------ detectors


def orbit_turn(points: list[tuple[datetime, float, float, float | None]],
               window: timedelta, max_gap: timedelta) -> tuple[float, int, int]:
    """Best sliding window over (ts, lat, lon, heading) sorted by ts:
    (cumulative |heading change| in degrees, start index, end index)."""
    n = len(points)
    if n < 3:
        return 0.0, 0, 0
    heads: list[float | None] = []
    for i, (_, la, lo, hd) in enumerate(points):
        if hd is not None:
            heads.append(float(hd))
        elif i + 1 < n:
            heads.append(_bearing(la, lo, points[i + 1][1], points[i + 1][2]))
        else:
            heads.append(heads[-1] if heads else None)
    # turn[i] = |change| between fix i-1 and i (0 across gaps / unknown headings)
    turn = [0.0] * n
    for i in range(1, n):
        a, b = heads[i - 1], heads[i]
        if a is not None and b is not None and points[i][0] - points[i - 1][0] <= max_gap:
            turn[i] = abs(_angle_diff(a, b))
    best, bi, bj = 0.0, 0, 0
    acc, i = 0.0, 0
    for j in range(n):
        acc += turn[j] if j > i else 0.0
        while points[j][0] - points[i][0] > window:
            i += 1
            acc -= turn[i]  # turn[i] links fix i-1 (now outside) to fix i
        if acc > best:
            best, bi, bj = acc, i, j
    return best, bi, bj


class SensorEventDetector:
    def __init__(self, store: SqliteStore, lake: Lake, metrics: Metrics | None = None,
                 config: SensorEventsConfig | None = None, *,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self.lake = lake
        self.metrics = metrics or Metrics()
        self.cfg = config or SensorEventsConfig.from_file()
        self.clock = clock
        self._baseline: dict[str, float] = {}
        self._baseline_at: datetime | None = None
        self._stop = asyncio.Event()
        self.passes = 0
        self.last: dict[str, Any] | None = None
        self.last_error: str | None = None

    @classmethod
    def from_settings(cls, settings: Any, store: SqliteStore, lake: Lake,
                      metrics: Metrics | None = None) -> SensorEventDetector:
        return cls(store, lake, metrics,
                   SensorEventsConfig.from_file(Path(settings.config_dir) / "corroboration.yaml"))

    # ---------------------------------------------------------------- pass

    def run_once(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        now = self.clock()
        found: dict[str, list[Detection]] = {}
        for name in DETECTORS:
            try:
                found[name] = getattr(self, f"detect_{name}")(now)
            except Exception:
                self.metrics.inc("sensor_events_errors_total", detector=name)
                log.exception("sensor detector %s failed", name)
                found[name] = []
        out = {"created": {}, "updated": {}, "detections": {}}
        for name, dets in found.items():
            out["detections"][name] = len(dets)
            for det in dets:
                kind = self.emit(det, now)
                if kind:
                    out[kind][name] = out[kind].get(name, 0) + 1
        for name, n in out["created"].items():
            self.metrics.inc("sensor_events_created_total", n, detector=name)
        out["seconds"] = round(time.perf_counter() - t0, 3)
        self.metrics.set("sensor_events_pass_seconds", out["seconds"])
        self.passes += 1
        self.last = out
        return out

    # ------------------------------------------------------------ orbit

    def _air_positions(self, since: datetime, until: datetime) -> list[tuple]:
        return self.lake.query(
            "SELECT track_id, epoch_us(ts), lat, lon, heading_deg, alt_ft, h3_3 FROM positions "
            "WHERE kind = 'air' AND ts >= ? AND ts <= ? ORDER BY track_id, ts",
            [since, until], start=since, end=until)

    def detect_orbit(self, now: datetime) -> list[Detection]:
        c = self.cfg.orbit
        rows = self._air_positions(now - timedelta(minutes=_g(c, "lookback_min", 120)), now)
        series: dict[str, list] = defaultdict(list)
        for track, us, lat, lon, hd, alt, _ in rows:
            if alt is not None and alt < 500:
                continue  # taxiing / parked
            series[track].append((ts_from_us(us), lat, lon, hd))
        info = self.store.get_tracks(sorted(series))
        types = {str(t).upper() for t in c.get("types") or ()}
        cats = {str(t) for t in c.get("categories") or ()}
        window = timedelta(minutes=_g(c, "window_min", 60))
        max_gap = timedelta(minutes=_g(c, "max_gap_min", 10))
        min_turn, max_r = _g(c, "min_turn_deg", 540), _g(c, "max_radius_km", 100)
        hits = []
        for track, pts in series.items():
            t = info.get(track) or {}
            if not t.get("is_mil") or not (
                    str(t.get("type_code") or "").upper() in types or t.get("category") in cats):
                continue
            if len(pts) < int(_g(c, "min_points", 8)):
                continue
            turn, i, j = orbit_turn(pts, window, max_gap)
            if turn < min_turn or j - i + 1 < int(_g(c, "min_points", 8)):
                continue
            win = pts[i:j + 1]
            clat, clon = _centroid([(p[1], p[2]) for p in win])
            radius = max(haversine_km(clat, clon, p[1], p[2]) for p in win)
            if radius > max_r:
                continue
            hits.append((track, t, turn, win, clat, clon, radius))
        by_cell: dict[str, list] = defaultdict(list)
        for h in hits:
            by_cell[_cell3(h[4], h[5])].append(h)
        dets = []
        for group in by_cell.values():
            group.sort(key=lambda h: -h[2])
            lat, lon = _centroid([(h[4], h[5]) for h in group])
            t_start = min(h[3][0][0] for h in group)
            t_end = max(h[3][-1][0] for h in group)
            best = group[0][2]
            hp = any(h[1].get("high_priority") for h in group)
            conf = min(0.95, 0.5 + 0.3 * min(1.0, (best - min_turn) / min_turn)
                       + 0.05 * (len(group) - 1) + (0.1 if hp else 0.0))
            names = [f"{h[1].get('type_code') or '?'} {h[1].get('name') or h[0]}"
                     for h in group]
            title = f"Military orbit: {names[0]}" + (
                f" (+{len(names) - 1} more)" if len(names) > 1 else "")
            evidence = [{
                "kind": "aircraft", "ref_id": h[0], "rule": "orbit",
                "distance_km": round(haversine_km(lat, lon, h[4], h[5]), 2),
                "dt_minutes": 0.0, "score": round(min(1.0, h[2] / (2 * min_turn)), 4),
                "details": {"type": h[1].get("type_code"), "callsign": h[1].get("name"),
                            "turn_deg": round(h[2]), "radius_km": round(h[6], 1),
                            "positions": len(h[3]), "start": iso(h[3][0][0]),
                            "end": iso(h[3][-1][0]),
                            "high_priority": bool(h[1].get("high_priority"))}}
                for h in group]
            dets.append(Detection(
                "orbit", "air_activity", lat, lon, t_start, t_end, round(conf, 4), title,
                f"{len(group)} military aircraft flying racetrack/loiter patterns "
                f"(max {round(best)} deg of turn in {int(window.total_seconds() // 60)} min): "
                + ", ".join(names), evidence,
                {"aircraft": len(group), "max_turn_deg": round(best)},
                merge_gap_h=_g(c, "merge_gap_h", 2)))
        return dets

    # ----------------------------------------------------- concentration

    def _refresh_baseline(self, now: datetime, start: datetime) -> None:
        c = self.cfg.concentration
        every = timedelta(minutes=_g(c, "baseline_refresh_min", 60))
        if self._baseline_at is not None and now - self._baseline_at < every:
            return
        lo = start - timedelta(days=_g(c, "baseline_days", 7))
        bucket_us = int(_g(c, "window_min", 30) * 60 * 1_000_000)
        rows = self.lake.query(
            "SELECT h3_3, count(DISTINCT track_id), epoch_us(min(ts)) FROM positions "
            "WHERE kind = 'air' AND ts >= ? AND ts < ? AND alt_ft >= ? "
            f"GROUP BY h3_3, epoch_us(ts) // {bucket_us}",
            [lo, start, int(_g(c, "min_alt_ft", 500))], start=lo, end=start)
        if not rows:
            self._baseline, self._baseline_at = {}, now
            return
        first = ts_from_us(min(r[2] for r in rows))
        buckets = max(1.0, (start - first).total_seconds() * 1e6 / bucket_us)
        sums: dict[str, float] = defaultdict(float)
        for cell, n, _ in rows:
            sums[h3.int_to_str(cell)] += n
        self._baseline = {k: v / buckets for k, v in sums.items()}
        self._baseline_at = now

    def detect_concentration(self, now: datetime) -> list[Detection]:
        c = self.cfg.concentration
        start = now - timedelta(minutes=_g(c, "window_min", 30))
        rows = self.lake.query(
            "SELECT track_id, epoch_us(max(ts)), arg_max(lat, ts), arg_max(lon, ts), "
            "arg_max(h3_3, ts) FROM positions WHERE kind = 'air' AND ts >= ? AND ts <= ? "
            "AND alt_ft >= ? GROUP BY track_id",
            [start, now, int(_g(c, "min_alt_ft", 500))], start=start, end=now)
        info = self.store.get_tracks(sorted(r[0] for r in rows))
        latest = {r[0]: r for r in rows if (info.get(r[0]) or {}).get("is_mil")}
        per_cell: dict[str, set[str]] = defaultdict(set)
        for tr, r in latest.items():
            per_cell[h3.int_to_str(r[4])].add(tr)
        if not per_cell:
            return []
        self._refresh_baseline(now, start)
        n_min = int(_g(c, "min_aircraft", 6))
        cands = []
        for cell in per_cell:
            disk = h3.grid_disk(cell, 1)
            tracks = set().union(*(per_cell.get(x, set()) for x in disk))
            base = sum(self._baseline.get(x, 0.0) for x in disk)
            if len(tracks) >= n_min and len(tracks) >= _g(c, "ratio", 2) * base + _g(
                    c, "min_excess", 3):
                cands.append((len(tracks), cell, tracks, base, set(disk)))
        cands.sort(key=lambda x: (-x[0], x[1]))
        taken: set[str] = set()
        dets = []
        for n, cell, tracks, base, disk in cands:
            if cell in taken:
                continue
            taken |= disk
            pts = [(latest[t][2], latest[t][3]) for t in tracks]
            lat, lon = _centroid(pts)
            types: dict[str, int] = defaultdict(int)
            for t in tracks:
                types[str(info[t].get("type_code") or "?")] += 1
            mix = ", ".join(f"{k}x{v}" for k, v in sorted(types.items(), key=lambda kv: -kv[1]))
            conf = min(0.95, 0.5 + 0.05 * (n - n_min)
                       + 0.15 * min(1.0, (n / max(base, 1.0)) / 4))
            evidence = [{
                "kind": "aircraft", "ref_id": t, "rule": "concentration",
                "distance_km": round(haversine_km(lat, lon, latest[t][2], latest[t][3]), 2),
                "dt_minutes": 0.0, "score": round(0.25 + (0.2 if info[t].get(
                    "high_priority") else 0.0), 4),
                "details": {"type": info[t].get("type_code"), "callsign": info[t].get("name"),
                            "last_seen": iso(ts_from_us(latest[t][1])),
                            "high_priority": bool(info[t].get("high_priority"))}}
                for t in sorted(tracks)]
            dets.append(Detection(
                "concentration", "air_activity", lat, lon, start, now, round(conf, 4),
                f"Military aircraft concentration: {n} aircraft",
                f"{n} airborne military aircraft within ~150 km in {int(_g(c, 'window_min', 30))}"
                f" min (7-day baseline {base:.1f}): {mix}", evidence,
                {"aircraft": n, "baseline": round(base, 2), "types": dict(types)},
                merge_gap_h=_g(c, "merge_gap_h", 1)))
        return dets

    # ------------------------------------------------------------ AIS gap

    def detect_ais_gap(self, now: datetime) -> list[Detection]:
        c = self.cfg.ais_gap
        since = now - timedelta(hours=_g(c, "lookback_h", 24))
        rows = self.lake.query(
            "SELECT track_id, epoch_us(ts), lat, lon, h3_3 FROM positions WHERE kind = 'sea' "
            "AND ts >= ? AND ts <= ? ORDER BY track_id, ts", [since, now], start=since, end=now)
        series: dict[str, list] = defaultdict(list)
        reports: dict[str, list[tuple[datetime, str]]] = defaultdict(list)  # cell -> (ts, track)
        for track, us, lat, lon, cell in rows:
            ts = ts_from_us(us)
            series[track].append((ts, lat, lon))
            reports[h3.int_to_str(cell)].append((ts, track))
        min_gap = timedelta(hours=_g(c, "min_gap_h", 6))
        max_kmh = _g(c, "max_drift_kts", 20) * _KTS_KMH
        need = int(_g(c, "min_coverage_tracks", 1))
        gaps = []
        for track, pts in series.items():
            for (t1, la1, lo1), (t2, la2, lo2) in pairwise(pts):
                if t2 - t1 < min_gap:
                    continue
                gap_h = (t2 - t1).total_seconds() / 3600
                if haversine_km(la1, lo1, la2, lo2) / gap_h > max_kmh:
                    continue
                cell = _cell3(la1, lo1)
                others = {tr for x in h3.grid_disk(cell, 1) for ts, tr in reports.get(x, ())
                          if t1 < ts < t2 and tr != track}
                if len(others) < need:
                    continue
                gaps.append((track, t1, t2, la1, lo1, la2, lo2, gap_h, len(others), cell))
        info = self.store.get_tracks(sorted({g[0] for g in gaps}))
        by_cell: dict[str, list] = defaultdict(list)
        for g in gaps:
            by_cell[g[9]].append(g)
        dets = []
        for group in by_cell.values():
            lat, lon = _centroid([(g[3], g[4]) for g in group])
            longest = max(g[7] for g in group)
            cover = max(g[8] for g in group)
            conf = min(0.9, 0.4 + 0.3 * min(1.0, (longest - min_gap.total_seconds() / 3600)
                                            / 12) + 0.2 * min(1.0, cover / 5))
            names = [(info.get(g[0]) or {}).get("name") or g[0] for g in group]
            evidence = [{
                "kind": "ais_gap", "ref_id": f"{g[0]}@{iso(g[1])}", "rule": "ais_gap",
                "distance_km": round(haversine_km(lat, lon, g[3], g[4]), 2),
                "dt_minutes": 0.0, "score": round(0.6 * min(1.0, g[7] / 6), 4),
                "details": {"track_id": g[0], "name": (info.get(g[0]) or {}).get("name"),
                            "gap_h": round(g[7], 2), "gap_start": iso(g[1]),
                            "gap_end": iso(g[2]), "last_lat": g[3], "last_lon": g[4],
                            "next_lat": g[5], "next_lon": g[6], "coverage_tracks": g[8]}}
                for g in group]
            dets.append(Detection(
                "ais_gap", "maritime_security", lat, lon, min(g[1] for g in group),
                max(g[2] for g in group), round(conf, 4),
                f"AIS dark gap: {names[0]}" + (f" (+{len(names) - 1} more)"
                                               if len(names) > 1 else ""),
                f"{len(group)} ship(s) stopped transmitting AIS for up to {longest:.1f} h while "
                f"{cover} other ship(s) kept reporting nearby", evidence,
                {"ships": len(group), "longest_gap_h": round(longest, 2)},
                merge_gap_h=_g(c, "min_gap_h", 6)))
        return dets

    # ------------------------------------------------------------ seismic

    def detect_seismic(self, now: datetime) -> list[Detection]:
        c = self.cfg.seismic
        types = {str(k).casefold(): float(v) for k, v in (c.get("types") or {}).items()}
        since = now - timedelta(hours=_g(c, "lookback_h", 48))
        rows = self.lake.query(
            "SELECT id, epoch_us(max(ts)), arg_max(lat, ts), arg_max(lon, ts), "
            "arg_max(value, ts), arg_max(attrs, ts) FROM observations WHERE type = 'quake' "
            "AND ts >= ? GROUP BY id", [since], start=since)
        dets = []
        for oid, us, lat, lon, mag, attrs_s in rows:
            try:
                attrs = json.loads(attrs_s) if attrs_s else {}
            except ValueError:
                attrs = {}
            etype = str(attrs.get("event_type") or "").casefold()
            if etype not in types:
                continue
            ts = ts_from_us(us)
            place = attrs.get("place") or f"{lat:.2f}, {lon:.2f}"
            mtxt = f"M{mag:.1f} " if mag is not None else ""
            dets.append(Detection(
                "seismic", str(c.get("category") or "infrastructure"), lat, lon, ts, ts,
                round(types[etype], 4), f"Seismic {etype}: {mtxt}{place}",
                f"USGS classified a {mtxt}event as '{etype}' ({place}).",
                [{"kind": "quake", "ref_id": oid, "rule": "seismic", "distance_km": 0.0,
                  "dt_minutes": 0.0, "score": round(types[etype], 4),
                  "details": {"magnitude": mag, "event_type": etype,
                              "depth_km": attrs.get("depth_km"), "url": attrs.get("url"),
                              "place": attrs.get("place")}}],
                {"event_type": etype, "magnitude": mag}, merge_gap_h=0.0,
                key=f"seismic:{_cell3(lat, lon)}:{oid}"))
        return dets

    # --------------------------------------------------------------- emit

    def emit(self, det: Detection, now: datetime) -> str | None:
        """Create or extend the event for ``det``: 'created' | 'updated' | None (no change)."""
        cell = _cell3(det.lat, det.lon)
        window_h = max(det.merge_gap_h, 1.0)
        bucket = int(det.t_start.timestamp() // (window_h * 3600))
        key = det.key or f"{det.detector}:{cell}:{bucket}"
        existing = self.store.get_sensor_event_by_key(key)
        if existing is None and det.merge_gap_h > 0:
            existing = self.store.find_sensor_event(
                det.detector, list(h3.grid_disk(cell, 1)),
                det.t_start - timedelta(hours=det.merge_gap_h))
        old = self.store.get_event(existing["event_id"]) if existing else None
        eid = existing["event_id"] if existing else new_ulid(now)
        t_start, t_end = det.t_start, det.t_end
        conf = det.confidence
        version = 1
        first_seen = now
        if old is not None:
            t_start = min(t_start, datetime.fromisoformat(old["t_start"]))
            t_end = max(t_end, datetime.fromisoformat(old["t_end"]))
            conf = max(conf, float(old["confidence"]))
            known = {(r["kind"], r["ref_id"]) for r in self.store.list_corroborations(eid)}
            fresh = [e for e in det.evidence if (e["kind"], e["ref_id"]) not in known]
            if (iso(t_start) == old["t_start"] and iso(t_end) == old["t_end"]
                    and conf == old["confidence"] and not fresh):
                return None
            version = int(old["version"]) + 1
            first_seen = datetime.fromisoformat(old["first_seen"])
        c3, c5, c7 = (h3.latlng_to_cell(det.lat, det.lon, r) for r in (3, 5, 7))
        n_evidence = len(det.evidence) if old is None else len(
            {(r["kind"], r["ref_id"]) for r in self.store.list_corroborations(eid)}
            | {(e["kind"], e["ref_id"]) for e in det.evidence})
        row = {
            "id": eid, "category": det.category, "subcategory": det.detector,
            "cameo_root": None, "title": det.title if old is None else old["title"],
            "summary": det.summary, "actors": [], "lat": det.lat if old is None else old["lat"],
            "lon": det.lon if old is None else old["lon"], "precision": "point",
            "place_name": f"{abs(det.lat):.2f}{'N' if det.lat >= 0 else 'S'} "
                          f"{abs(det.lon):.2f}{'E' if det.lon >= 0 else 'W'}"
            if old is None else old["place_name"],
            "country": None, "geo_confidence": 1.0, "h3_3": c3, "h3_5": c5, "h3_7": c7,
            "t_start": iso(t_start), "t_end": iso(t_end), "status": "sensor-corroborated",
            "confidence": round(conf, 4), "version": version, "mention_count": n_evidence,
            "source_diversity": 0, "gdelt_ids": [], "first_seen": iso(first_seen),
            "updated_at": iso(now),
            "cluster_state": {"sensor": True, "detector": det.detector,
                              "sensor_corroborated": True, "v": CLUSTER_VERSION},
        }
        if old is not None:  # keep the original cells with the original point
            row["h3_3"], row["h3_5"], row["h3_7"] = old["h3_3"], old["h3_5"], old["h3_7"]
        evidence = [{**e, "event_id": eid} for e in det.evidence]
        kinds = sorted({e["kind"] for e in det.evidence})
        new = self.store.save_sensor_event(
            row, {"detector": det.detector, "dedupe_key": key if not existing
                  else existing["dedupe_key"], "cell": cell if not existing
                  else existing["cell"], "details": det.details},
            evidence, {"event_id": eid, "score": round(conf, 4), "kinds": kinds,
                       "evidence": n_evidence, "corroborated": True})
        for kind, n in new.items():
            self.metrics.inc("corroborations_total", n, kind=kind)
        return "created" if old is None else "updated"

    # --------------------------------------------------------------- loop

    async def run(self) -> None:
        self._stop.clear()
        try:  # let boot (ingest warm start, first collector polls) settle first
            await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.initial_delay_s)
        except TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.run_once)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                self.metrics.inc("sensor_events_errors_total", detector="pass")
                log.exception("sensor events pass failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.interval_s)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


def wire(runtime: Runtime) -> SensorEventDetector:
    """Create ``runtime.sensor_events`` and return it; the caller schedules ``run()``."""
    det = SensorEventDetector.from_settings(runtime.settings, runtime.store, runtime.lake,
                                            runtime.metrics)
    runtime.sensor_events = det
    return det
