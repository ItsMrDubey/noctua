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
  and above the same-hour-of-day baseline (mean + ``sigma`` x std, >= ``ratio`` x mean,
  + ``min_excess``) over the previous ``baseline_days`` (7) in those cells (all air tracks,
  i.e. conservative), refreshed hourly. Trainers are not counted; < ``min_baseline_days`` (3)
  of baseline data means no event; within ``airfield_radius_km`` of a military airfield the
  excess must be >= ``airfield_ratio`` x baseline. Overlapping areas favour the densest cell.
  The orbit detector ignores holding patterns (low altitude near a large/medium airport,
  unless > 45 min), requires a >= 30 km long axis, and counts dual-use airliner types
  (A332...) only with a military callsign/operator marker.
* ``ais_gap`` — a ship whose consecutive AIS positions are >= ``min_gap_h`` (6 h) apart,
  with an implied speed across the gap <= ``max_drift_kts`` (else a bad fix), while at least
  ``min_coverage_tracks`` other ships kept reporting in the same res-3 area during the gap
  (so the silence is not simply missing receiver coverage).
* ``seismic`` — USGS events typed explosion / chemical / nuclear / mining explosion / quarry
  blast; confidence per type.
* ``hazard`` — official natural-hazard alerts (``hazard`` observations from GDACS, NASA
  EONET, ReliefWeb; ``osint.core.hazards``) at or above ``min_alert`` for their type, status
  ``official``, confidence from the GDACS alert level / per-source value. Reports of one
  hazard from several sources (same type, within ``group_km`` and overlapping in time, or
  sharing a GLIDE number / country for country-level records) become one event with every
  source as evidence; a GDACS/EONET earthquake that matches a USGS quake (``quake_match``)
  is keyed and placed on the USGS event and carries it as ``quake`` evidence (ShakeMap
  areas via the impact job), so the same quake is never two events. Per-type detector names
  ``hazard_<type>`` keep the area merge (``merge_gap_h``) within one hazard type.

Every event gets category air_activity / maritime_security (seismic: configurable; hazard:
disaster), status ``sensor-corroborated`` by definition (hazard: ``official``), ``confidence`` from the pattern strength, and its
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
import re
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
from osint.core.hazards import ALERT_LEVELS, HAZARD_TYPES, parse_time
from osint.core.metrics import Metrics
from osint.enrich.mil_types import TRAINERS
from osint.pipeline.cluster import CLUSTER_VERSION, new_ulid
from osint.pipeline.corroborate import ts_from_us
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore, iso

if TYPE_CHECKING:
    from osint.runtime import Runtime

log = logging.getLogger(__name__)

DEFAULT_CONFIG = ROOT / "config" / "corroboration.yaml"
DETECTORS = ("orbit", "concentration", "ais_gap", "seismic", "hazard")
# bumped when a detector's rules change; on start, events written by an older version are
# deleted (re-detected under the current rules if the pattern still holds).
# concentration v2: baseline counts only buckets with collected positions, civil GA types
# excluded (removes "Military aircraft concentration: 6/7 aircraft" false positives)
DETECTOR_VERSIONS = {"orbit": 1, "concentration": 2, "ais_gap": 1, "seismic": 1,
                     **{f"hazard_{t}": 1 for t in HAZARD_TYPES}}
_KTS_KMH = 1.852
SOURCE_NAMES = {"gdacs": "GDACS", "eonet": "NASA EONET", "reliefweb": "ReliefWeb"}


@dataclass
class SensorEventsConfig:
    interval_s: float = 300.0
    initial_delay_s: float = 60.0
    orbit: dict[str, Any] = field(default_factory=dict)
    concentration: dict[str, Any] = field(default_factory=dict)
    ais_gap: dict[str, Any] = field(default_factory=dict)
    seismic: dict[str, Any] = field(default_factory=dict)
    hazard: dict[str, Any] = field(default_factory=dict)

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


def _long_axis_km(win: list[tuple]) -> float:
    pts = win[::max(1, len(win) // 60)]
    return max((haversine_km(a[1], a[2], b[1], b[2]) for a in pts for b in pts), default=0.0)


def _severity(text: object) -> str:
    """Upstream severity text, without GDACS's placeholder for floods ("Magnitude 0")."""
    t = " ".join(str(text or "").split())
    return "" if re.fullmatch(r"(?i)magnitude 0(\.0+)?", t) else t


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
    subcategory: str | None = None  # default: the detector name
    status: str = "sensor-corroborated"
    precision: str = "point"
    place_name: str | None = None  # default: the coordinates
    country: str | None = None


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
                 clock: Callable[[], datetime] = utcnow,
                 airports: list[dict] | None = None,
                 bases: list[dict] | None = None) -> None:
        self.store = store
        self.lake = lake
        self.metrics = metrics or Metrics()
        self.cfg = config or SensorEventsConfig.from_file()
        self.clock = clock
        self._airports, self._bases = airports, bases
        self._sites_cache: tuple[list, list] | None = None
        self._baseline: dict[str, dict[tuple[int, int], dict[int, int]]] = {}
        # (day, hour, slot) buckets in which any air position was collected at all
        self._coverage: set[tuple[int, int, int]] = set()
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

    def _sites(self) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """(large/medium airports, military airfields) as (lat, lon); lazily from the
        reference data unless injected. Missing reference data -> empty (no suppression)."""
        if self._sites_cache is None:
            airports, bases = self._airports, self._bases
            if airports is None or bases is None:
                from osint.reference.loader import default_dir, load_airports, load_bases
                if airports is None:
                    airports = load_airports(default_dir() / "airports.csv")
                if bases is None:
                    bases = load_bases()
            civil = [(a["lat"], a["lon"]) for a in airports
                     if a.get("type") in ("large_airport", "medium_airport")]
            mil = [(a["lat"], a["lon"]) for a in airports if a.get("is_military")]
            mil += [(b["lat"], b["lon"]) for b in bases if b.get("type") == "airbase"]
            self._sites_cache = (civil, mil)
        return self._sites_cache

    def detect_orbit(self, now: datetime) -> list[Detection]:
        c = self.cfg.orbit
        rows = self._air_positions(now - timedelta(minutes=_g(c, "lookback_min", 120)), now)
        series: dict[str, list] = defaultdict(list)
        alts: dict[str, list] = defaultdict(list)
        for track, us, lat, lon, hd, alt, _ in rows:
            if alt is not None and alt < 500:
                continue  # taxiing / parked
            series[track].append((ts_from_us(us), lat, lon, hd))
            alts[track].append(alt)
        info = self.store.get_tracks(sorted(series))
        types = {str(t).upper() for t in c.get("types") or ()}
        cats = {str(t) for t in c.get("categories") or ()}
        window = timedelta(minutes=_g(c, "window_min", 60))
        max_gap = timedelta(minutes=_g(c, "max_gap_min", 10))
        min_turn, max_r = _g(c, "min_turn_deg", 540), _g(c, "max_radius_km", 100)
        dual = {str(t).upper() for t in c.get("dual_use_types") or ()}
        mil_re = re.compile(str(c.get("mil_marker_re") or r"$^"), re.IGNORECASE)
        airports, _ = self._sites()
        hits = []
        for track, pts in series.items():
            t = info.get(track) or {}
            tcode = str(t.get("type_code") or "").upper()
            if not t.get("is_mil") or not (tcode in types or t.get("category") in cats):
                continue
            if tcode in dual and not mil_re.search(
                    f"{t.get('name') or ''} {t.get('operator') or ''}"):
                continue  # A332 etc. are airliners unless callsign/operator says MRTT / air force
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
            if _long_axis_km(win) < _g(c, "min_axis_km", 30):
                continue  # too small for an AAR / ISR orbit (holding patterns, circuits)
            tail = alts[track][i:j + 1]
            known = sorted(a for a in tail if a is not None)
            alt = known[len(known) // 2] if known else None
            if alt is not None and alt < _g(c, "airport_alt_ft", 20000) and any(
                    haversine_km(clat, clon, a[0], a[1]) <= _g(c, "airport_radius_km", 60)
                    for a in airports):
                long_enough = (win[-1][0] - win[0][0]).total_seconds() / 60 > _g(
                    c, "airport_min_duration_min", 45)
                if not long_enough:  # (types here are already ISR / tanker, mil-checked)
                    continue  # airport holding stack
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
        """cell -> {(day, hour): {slot: distinct aircraft}} over the previous baseline_days
        (all air tracks: conservative, >= the military-only baseline); refreshed hourly."""
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
        base: dict[str, dict[tuple[int, int], dict[int, int]]] = defaultdict(
            lambda: defaultdict(dict))
        day_us, hour_us = 86_400_000_000, 3_600_000_000
        coverage: set[tuple[int, int, int]] = set()
        for cell, n, us in rows:
            slot = (us % hour_us) // bucket_us
            day, hour = us // day_us, (us % day_us) // hour_us
            base[h3.int_to_str(cell)][(day, hour)][slot] = n
            coverage.add((day, hour, slot))
        self._baseline, self._coverage, self._baseline_at = base, coverage, now

    def _baseline_stats(self, disk: set[str], hour: int) -> tuple[float, float, int]:
        """(mean, std, days) of the distinct-aircraft count in ``disk`` per window bucket at
        this hour of day, over the days on which the area has any baseline data - counting
        only buckets in which positions were actually collected (a collector outage is
        not "zero aircraft", which would deflate the baseline)."""
        days: set[int] = set()
        totals: dict[tuple[int, int], float] = defaultdict(float)  # (day, slot) -> count
        for cell in disk:
            for (day, hr), per_slot in self._baseline.get(cell, {}).items():
                days.add(day)
                if hr == hour:
                    for slot, n in per_slot.items():
                        totals[(day, slot)] += n
        buckets = sorted((d, s) for d, hr, s in self._coverage if hr == hour and d in days)
        if not buckets:
            return 0.0, 0.0, 0
        vals = [totals.get(b, 0.0) for b in buckets]
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        return mean, math.sqrt(var), len({d for d, _ in buckets})

    def detect_concentration(self, now: datetime) -> list[Detection]:
        c = self.cfg.concentration
        start = now - timedelta(minutes=_g(c, "window_min", 30))
        rows = self.lake.query(
            "SELECT track_id, epoch_us(max(ts)), arg_max(lat, ts), arg_max(lon, ts), "
            "arg_max(h3_3, ts) FROM positions WHERE kind = 'air' AND ts >= ? AND ts <= ? "
            "AND alt_ft >= ? GROUP BY track_id",
            [start, now, int(_g(c, "min_alt_ft", 500))], start=start, end=now)
        info = self.store.get_tracks(sorted(r[0] for r in rows))
        skip = {str(t).upper() for t in c.get("exclude_types") or ()} | TRAINERS

        def counted(t: dict | None) -> bool:
            t = t or {}
            return bool(t.get("is_mil")) and t.get("category") != "Trainer" and str(
                t.get("type_code") or "").upper() not in skip

        latest = {r[0]: r for r in rows if counted(info.get(r[0]))}
        per_cell: dict[str, set[str]] = defaultdict(set)
        for tr, r in latest.items():
            per_cell[h3.int_to_str(r[4])].add(tr)
        if not per_cell:
            return []
        self._refresh_baseline(now, start)
        hour = (start + (now - start) / 2).hour
        n_min = int(_g(c, "min_aircraft", 6))
        min_days = int(_g(c, "min_baseline_days", 3))
        sigma, ratio = _g(c, "sigma", 3), _g(c, "ratio", 2)
        _, mil_fields = self._sites()
        cands = []
        for cell in per_cell:
            disk = h3.grid_disk(cell, 1)
            tracks = set().union(*(per_cell.get(x, set()) for x in disk))
            n = len(tracks)
            if n < n_min:
                continue
            base, std, days = self._baseline_stats(set(disk), hour)
            if days < min_days:
                continue  # no usable baseline: cannot call it unusual
            if n <= base + sigma * std or n < ratio * base or n < base + _g(
                    c, "min_excess", 3):
                continue
            clat, clon = _centroid([(latest[t][2], latest[t][3]) for t in tracks])
            if any(haversine_km(clat, clon, f[0], f[1]) <= _g(c, "airfield_radius_km", 30)
                   for f in mil_fields) and n < _g(c, "airfield_ratio", 3) * base:
                continue  # busy military airfield: only a large surge counts
            cands.append((n, cell, tracks, base, set(disk)))
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
                f" min (same-hour baseline {base:.1f}): {mix}", evidence,
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

    # ------------------------------------------------------------- hazard

    def _usgs_match(self, c: dict, h: dict, quakes: list[tuple]) -> tuple | None:
        """The USGS quake (id, ts, lat, lon, mag, attrs) that is this hazard earthquake."""
        q = c.get("quake_match") or {}
        radius, minutes = _g(q, "radius_km", 100), _g(q, "minutes", 15)
        max_dmag = _g(q, "max_mag_diff", 0.7)
        best = None
        for row in quakes:
            d = haversine_km(h["lat"], h["lon"], row[2], row[3])
            dt = abs((row[1] - h["t_start"]).total_seconds()) / 60
            if d > radius or dt > minutes:
                continue
            if h["value"] is not None and row[4] is not None \
                    and abs(h["value"] - row[4]) > max_dmag:
                continue
            score = d / radius + dt / minutes
            if best is None or score < best[0]:
                best = (score, row)
        return best[1] if best else None

    @staticmethod
    def _same_hazard(c: dict, a: dict, b: dict) -> bool:
        """Do two records of one hazard type (different sources) describe one hazard?"""
        ga, gb = a["attrs"].get("glide"), b["attrs"].get("glide")
        if ga and gb:
            return ga == gb
        if "country" in (a["precision"], b["precision"]):
            if not a["iso3"] & b["iso3"]:
                return False
        else:
            km = c.get("group_km") or {}
            if haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]) > float(
                    km.get(a["hazard"], km.get("default", 150))):
                return False
        slack = timedelta(hours=_g(c, "group_slack_h", 72))
        return a["t_start"] - slack <= b["t_end"] and b["t_start"] - slack <= a["t_end"]

    def _hazard_records(self, c: dict, since: datetime, now: datetime) -> list[dict]:
        rows = self.lake.query(
            "SELECT id, epoch_us(max(ts)), arg_max(lat, ts), arg_max(lon, ts), "
            "arg_max(value, ts), arg_max(attrs, ts), arg_max(source, ts) FROM observations "
            "WHERE type = 'hazard' AND ts >= ? GROUP BY id ORDER BY id", [since], start=since)
        min_alert = {str(k): str(v).casefold() for k, v in (c.get("min_alert") or {}).items()}
        alert_conf = {str(k).casefold(): float(v)
                      for k, v in (c.get("alert_confidence") or {}).items()}
        source_conf = {str(k): float(v) for k, v in (c.get("source_confidence") or {}).items()}
        exclude = {str(x) for x in c.get("exclude") or ()}
        out = []
        for oid, us, lat, lon, value, attrs_s, source in rows:
            try:
                attrs = json.loads(attrs_s) if attrs_s else {}
            except ValueError:
                continue
            hazard = str(attrs.get("hazard") or "other")
            if hazard in exclude:
                continue
            alert = str(attrs.get("alert_level") or "").casefold()
            need = min_alert.get(hazard, min_alert.get("default", "green"))
            if alert in ALERT_LEVELS and need in ALERT_LEVELS \
                    and ALERT_LEVELS.index(alert) < ALERT_LEVELS.index(need):
                continue
            ts = ts_from_us(us)
            t_start = min(now, parse_time(attrs.get("t_start")) or ts)
            t_end = min(now, max(t_start, parse_time(attrs.get("t_end")) or ts))
            conf = alert_conf[alert] if alert in alert_conf else source_conf.get(
                str(source), 0.6)
            iso3 = {str(x).upper() for x in [attrs.get("iso3"), *(attrs.get("iso3s") or [])]
                    if x}
            out.append({"id": oid, "lat": lat, "lon": lon, "value": value, "attrs": attrs,
                        "source": str(source), "hazard": hazard, "alert": alert or None,
                        "conf": conf, "t_start": t_start, "t_end": t_end, "iso3": iso3,
                        "precision": attrs.get("precision") or "point"})
        return out

    def _usgs_quakes(self, since: datetime) -> list[tuple]:
        out = []
        for qid, qus, qlat, qlon, qmag, qattrs in self.lake.query(
                "SELECT id, epoch_us(max(ts)), arg_max(lat, ts), arg_max(lon, ts), "
                "arg_max(value, ts), arg_max(attrs, ts) FROM observations "
                "WHERE type = 'quake' AND ts >= ? GROUP BY id", [since], start=since):
            try:
                qa = json.loads(qattrs) if qattrs else {}
            except ValueError:
                qa = {}
            out.append((qid, ts_from_us(qus), qlat, qlon, qmag, qa))
        return out

    def detect_hazard(self, now: datetime) -> list[Detection]:
        c = self.cfg.hazard
        since = now - timedelta(hours=_g(c, "lookback_h", 48))
        records = self._hazard_records(c, since, now)
        # one group per hazard: point records first, strongest first, at most one record
        # per source (GDACS lists neighbouring wildfires separately; they stay separate)
        rank = {s: i for i, s in enumerate(c.get("source_priority")
                                           or ["gdacs", "eonet", "reliefweb"])}
        records.sort(key=lambda r: (r["precision"] != "point", -r["conf"],
                                    rank.get(r["source"], 99), r["id"]))
        groups: list[list[dict]] = []
        for r in records:
            for g in groups:
                if g[0]["hazard"] == r["hazard"] and r["source"] not in {
                        x["source"] for x in g} and self._same_hazard(c, g[0], r):
                    g.append(r)
                    break
            else:
                groups.append([r])
        quakes = self._usgs_quakes(since - timedelta(hours=_g(c, "quake_lookback_h", 48))) \
            if any(g[0]["hazard"] == "earthquake" for g in groups) else []
        merge = c.get("merge_gap_h") or {}
        bonus = _g(c, "multi_source_bonus", 0.05)
        dets = []
        for g in groups:
            head, a = g[0], g[0]["attrs"]
            hazard = head["hazard"]
            lat, lon, key = head["lat"], head["lon"], f"hazard:{head['id']}"
            evidence = [{
                "kind": "hazard", "ref_id": r["id"],
                "rule": f"{r['source']}_{r['alert'] or 'alert'}",
                "distance_km": round(haversine_km(lat, lon, r["lat"], r["lon"]), 2),
                "dt_minutes": 0.0, "score": round(r["conf"], 4),
                "details": {k: r["attrs"].get(k) for k in (
                    "source", "hazard", "name", "alert_level", "severity_text", "url",
                    "glide", "country", "t_start", "t_end", "updated_at", "precision")}
                | {"value": r["value"]}} for r in g]
            quake = self._usgs_match(c, head, quakes) if hazard == "earthquake" else None
            precision = "point" if head["precision"] == "point" else "country"
            if quake is not None:  # the USGS event is the canonical one (key and place)
                lat, lon, key, precision = quake[2], quake[3], f"hazard:{quake[0]}", "point"
                evidence.append({
                    "kind": "quake", "ref_id": quake[0], "rule": "usgs_match",
                    "distance_km": round(haversine_km(head["lat"], head["lon"], lat, lon), 2),
                    "dt_minutes": round((quake[1] - head["t_start"]).total_seconds() / 60, 1),
                    "score": 1.0,
                    "details": {"magnitude": quake[4], "place": quake[5].get("place"),
                                "depth_km": quake[5].get("depth_km"),
                                "url": quake[5].get("url")}})
            conf = min(0.97, max(r["conf"] for r in g) + bonus * (len(g) - 1)
                       + (bonus if quake is not None else 0.0))
            label = hazard.replace("_", " ")
            name = " ".join(str(a.get("name") or "").split()) or label.capitalize()
            sources = sorted({r["source"] for r in g}, key=lambda s: (rank.get(s, 99), s))
            alert = next((r["alert"] for r in g if r["alert"]), None)
            facts = [f"{alert.capitalize()} alert" if alert else "",
                     _severity(a.get("severity_text")),
                     f"USGS M{quake[4]:.1f} {quake[5].get('place') or ''}".strip()
                     if quake is not None and quake[4] is not None else ""]
            iso2 = a.get("iso2") or []
            dets.append(Detection(
                f"hazard_{hazard}", str(c.get("category") or "disaster"), lat, lon,
                min(r["t_start"] for r in g), max(r["t_end"] for r in g), round(conf, 4),
                name, "; ".join(f for f in [label.capitalize(), *facts] if f)
                + f". Source: {', '.join(SOURCE_NAMES.get(s, s) for s in sources)}.",
                evidence, {"hazard": hazard, "alert_level": alert, "sources": sources,
                           "usgs_id": quake[0] if quake is not None else None,
                           "url": a.get("url")},
                merge_gap_h=float(merge.get(hazard, merge.get("default", 48))), key=key,
                subcategory=hazard, status=str(c.get("status") or "official"),
                precision=precision,
                place_name=(quake[5].get("place") if quake is not None else None)
                or a.get("country") or None,
                country=iso2[0] if len(iso2) == 1 else None))
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
            "id": eid, "category": det.category,
            "subcategory": det.subcategory or det.detector,
            "cameo_root": None, "title": det.title if old is None else old["title"],
            "summary": det.summary, "actors": [], "lat": det.lat if old is None else old["lat"],
            "lon": det.lon if old is None else old["lon"],
            "precision": det.precision if old is None else old["precision"],
            "place_name": (det.place_name or f"{abs(det.lat):.2f}{'N' if det.lat >= 0 else 'S'} "
                           f"{abs(det.lon):.2f}{'E' if det.lon >= 0 else 'W'}")
            if old is None else old["place_name"],
            "country": det.country if old is None else old.get("country"),
            "geo_confidence": 1.0 if det.precision == "point" else 0.5,
            "h3_3": c3, "h3_5": c5, "h3_7": c7,
            "t_start": iso(t_start), "t_end": iso(t_end), "status": det.status,
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
                  else existing["cell"], "details": {
                      **det.details,
                      "detector_version": DETECTOR_VERSIONS.get(det.detector, 1)}},
            evidence, {"event_id": eid, "score": round(conf, 4), "kinds": kinds,
                       "evidence": n_evidence, "corroborated": True})
        for kind, n in new.items():
            self.metrics.inc("corroborations_total", n, kind=kind)
        return "created" if old is None else "updated"

    # --------------------------------------------------------------- loop

    def purge_stale(self) -> int:
        """Delete sensor events written by an older detector version (DETECTOR_VERSIONS)."""
        ids = self.store.stale_sensor_event_ids(DETECTOR_VERSIONS)
        n = self.store.delete_events(ids, origin="sensor") if ids else 0
        if n:
            log.info("sensor events: removed %d events of older detector versions", n)
        return n

    async def run(self) -> None:
        self._stop.clear()
        try:
            await asyncio.to_thread(self.purge_stale)
        except Exception:
            log.exception("sensor events: purging stale detector versions failed")
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
