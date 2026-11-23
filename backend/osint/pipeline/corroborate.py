"""Sensor corroboration of news events (pipeline stage 6, spec §5; principle 3: H3).

For each event that needs (re-)evaluation, the per-category rules in
``config/corroboration.yaml`` look for sensor evidence near the event in space and time:

* ``fire``     FIRMS hotspots, scored by FIRMS confidence x FRP x proximity x timing
* ``quake``    USGS explosion / quarry-blast types or shallow small quakes (conflict,
               infrastructure) or magnitude (disaster)
* ``aircraft`` military aircraft tracks from the positions lake (high-priority types count
               more)
* ``ship``     ships in range (military ships and loitering count more)
* ``ais_gap``  AIS dark gaps: consecutive positions of a ship > ``min_gap_h`` apart, the gap
               overlapping the event window and an end of the gap within range
* ``hazard``   official hazard alerts (GDACS / NASA EONET / ReliefWeb ``hazard``
               observations) of a matching type, scored by alert level x proximity x timing

Candidate lookup never scans: the event point is turned into an H3 cell (res 5 when the
radius fits in a small ``grid_disk``, else res 3) plus ``grid_disk(k)`` covering the radius;
the lake is queried on the time window (hourly partition pruning) with an IN-list on
``h3_5``/``h3_3``; haversine only runs on those candidates.

Scores: every evidence row has a score in 0..1 (x rule weight x the event's precision
factor). Per kind the strongest ``top_per_kind`` rows combine by noisy-OR
(1 - prod(1 - s)); the event score is the noisy-OR of the kind scores. An event whose score
reaches ``threshold`` becomes ``sensor-corroborated``; the score is also the ``sensor``
term of the confidence formula. Country-precision events are skipped; admin1 is discounted.

Incremental: a pass evaluates only events updated within ``reeval_window_h`` that changed
since they were last evaluated, or whose H3 area received new positions/observations since
the previous pass.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import h3
import yaml

from osint.config import ROOT
from osint.core.geo import haversine_km, utcnow
from osint.core.metrics import Metrics
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore, iso

log = logging.getLogger(__name__)

DEFAULT_CONFIG = ROOT / "config" / "corroboration.yaml"
_WORDS = re.compile(r"[^\W_]+")
MANAGED_KINDS = frozenset({"fire", "quake", "aircraft", "ship", "ais_gap", "hazard"})
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def ts_from_us(us: int) -> datetime:
    return _EPOCH + timedelta(microseconds=int(us))


# ---------------------------------------------------------------------------- config


@dataclass(frozen=True)
class Rule:
    kind: str
    radius_km: float
    before_h: float
    after_h: float
    weight: float = 1.0
    keywords: tuple[str, ...] = ()
    params: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Rule:
        base = {"kind", "radius_km", "before_h", "after_h", "weight", "keywords"}
        return cls(kind=str(d["kind"]), radius_km=float(d["radius_km"]),
                   before_h=float(d.get("before_h", 6)), after_h=float(d.get("after_h", 6)),
                   weight=float(d.get("weight", 1.0)),
                   keywords=tuple(str(k).casefold() for k in d.get("keywords") or ()),
                   params={k: v for k, v in d.items() if k not in base})

    def applies(self, words: list[str]) -> bool:
        """Keywords are word prefixes ("strike" matches "strikes", not "ceasefire")."""
        if not self.keywords:
            return True
        return any(w.startswith(k) for w in words for k in self.keywords)


@dataclass
class CorroborationConfig:
    interval_s: float = 300.0
    reeval_window_h: float = 48.0
    threshold: float = 0.5
    top_per_kind: int = 5
    max_rows_per_kind: int = 25
    precision_factor: dict[str, float] = field(default_factory=lambda: {
        "point": 1.0, "city": 1.0, "admin1": 0.5, "country": 0.0})
    res5_max_k: int = 6
    categories: dict[str, list[Rule]] = field(default_factory=dict)
    scoring: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def s(self, key: str, default: float) -> float:
        return float(self.scoring.get(key, default))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CorroborationConfig:
        base = cls()
        return cls(
            interval_s=float(d.get("interval_s", base.interval_s)),
            reeval_window_h=float(d.get("reeval_window_h", base.reeval_window_h)),
            threshold=float(d.get("threshold", base.threshold)),
            top_per_kind=int(d.get("top_per_kind", base.top_per_kind)),
            max_rows_per_kind=int(d.get("max_rows_per_kind", base.max_rows_per_kind)),
            precision_factor={str(k): float(v) for k, v in
                              (d.get("precision_factor") or base.precision_factor).items()},
            res5_max_k=int((d.get("h3") or {}).get("res5_max_k", base.res5_max_k)),
            categories={str(c): [Rule.from_dict(r) for r in rules or ()]
                        for c, rules in (d.get("categories") or {}).items()},
            scoring=dict(d.get("scoring") or {}),
            raw=d,
        )

    @classmethod
    def from_file(cls, path: Path = DEFAULT_CONFIG) -> CorroborationConfig:
        if not path.is_file():
            log.warning("corroboration config %s missing; no rules", path)
            return cls()
        with path.open(encoding="utf-8") as f:
            return cls.from_dict(yaml.safe_load(f) or {})


# ------------------------------------------------------------------------------- H3


def _cell_radius_km(cell: str) -> float:
    """Smallest centre-to-vertex distance of ``cell`` (its edge length, ~circumradius)."""
    clat, clon = h3.cell_to_latlng(cell)
    return min(haversine_km(clat, clon, la, lo) for la, lo in h3.cell_to_boundary(cell))


def disk_k(radius_km: float, edge_km: float) -> int:
    """grid_disk k whose cells cover every point within ``radius_km`` of any point in the
    centre cell: a cell at grid distance k+1 is >= 1.5(k+1)e - 2e away."""
    return max(1, math.ceil((radius_km + 2 * edge_km) / (1.5 * edge_km)))


def search_cells(lat: float, lon: float, radius_km: float, res5_max_k: int = 6
                 ) -> tuple[int, set[int]]:
    """(resolution, H3 cells as uint64) whose union covers the circle (lat, lon, radius)."""
    for res in (5, 3):
        cell = h3.latlng_to_cell(lat, lon, res)
        # cells shrink away from the icosahedron face centres; 0.9 keeps a margin
        k = disk_k(radius_km, 0.9 * _cell_radius_km(cell))
        if res == 3 or k <= res5_max_k:
            return res, {h3.str_to_int(c) for c in h3.grid_disk(cell, k)}
    raise AssertionError("unreachable")


def _in_list(cells: Iterable[int]) -> str:
    return ",".join(str(int(c)) for c in sorted(cells)) or "NULL"


def noisy_or(scores: Iterable[float]) -> float:
    p = 1.0
    for s in scores:
        p *= 1.0 - min(1.0, max(0.0, s))
    return 1.0 - p


def rollup(rows: Iterable[dict], top: int) -> tuple[float, list[str]]:
    """Event score and kinds from evidence rows ({kind, score})."""
    by_kind: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r["score"] > 0:
            by_kind[r["kind"]].append(float(r["score"]))
    kind_scores = [noisy_or(sorted(v, reverse=True)[:top]) for v in by_kind.values()]
    return round(noisy_or(kind_scores), 4), sorted(by_kind)


# -------------------------------------------------------------------- evaluation


@dataclass
class _Req:
    """One (event, rule) lookup."""

    event_id: str
    lat: float
    lon: float
    t_start: datetime
    t_end: datetime
    rule: Rule
    factor: float
    res: int
    cells: set[int]

    @property
    def win_start(self) -> datetime:
        return self.t_start - timedelta(hours=self.rule.before_h)

    @property
    def win_end(self) -> datetime:
        return self.t_end + timedelta(hours=self.rule.after_h)

    def dt_minutes(self, ts: datetime) -> float:
        if ts < self.t_start:
            return round((ts - self.t_start).total_seconds() / 60, 1)
        if ts > self.t_end:
            return round((ts - self.t_end).total_seconds() / 60, 1)
        return 0.0

    def timing(self, ts: datetime) -> float:
        """1 inside [t_start, t_end], down to 0.5 at the window edges."""
        dt = self.dt_minutes(ts)
        if dt < 0:
            return 1.0 - 0.5 * min(1.0, -dt / 60 / max(self.rule.before_h, 1e-9))
        if dt > 0:
            return 1.0 - 0.5 * min(1.0, dt / 60 / max(self.rule.after_h, 1e-9))
        return 1.0

    def proximity(self, d_km: float) -> float:
        return 1.0 - 0.5 * min(1.0, d_km / self.rule.radius_km)


class _CellIndex:
    """Requests keyed by the H3 cells they cover, at res 3 and res 5."""

    def __init__(self, reqs: list[_Req]) -> None:
        self.reqs = reqs
        self.by3: dict[int, list[_Req]] = defaultdict(list)
        self.by5: dict[int, list[_Req]] = defaultdict(list)
        for r in reqs:
            idx = self.by5 if r.res == 5 else self.by3
            for c in r.cells:
                idx[c].append(r)

    def where(self) -> str:
        parts = []
        if self.by5:
            parts.append(f"h3_5 IN ({_in_list(self.by5)})")
        if self.by3:
            parts.append(f"h3_3 IN ({_in_list(self.by3)})")
        return "(" + " OR ".join(parts or ["FALSE"]) + ")"

    def window(self, pad: timedelta = timedelta()) -> tuple[datetime, datetime]:
        return (min(r.win_start for r in self.reqs) - pad,
                max(r.win_end for r in self.reqs) + pad)

    def match(self, h3_3: int | None, h3_5: int | None, lat: float, lon: float,
              ts: datetime | None, pad: timedelta = timedelta()):
        """(req, distance_km) for every request whose cells, window and radius hold."""
        cand = self.by5.get(h3_5, []) + self.by3.get(h3_3, [])
        for r in cand:
            if ts is not None and not (r.win_start - pad <= ts <= r.win_end + pad):
                continue
            d = haversine_km(r.lat, r.lon, lat, lon)
            if d <= r.rule.radius_km:
                yield r, d


@dataclass
class CorroborationResult:
    evaluated: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    new_rows: dict[str, int] = field(default_factory=dict)
    changed: list[str] = field(default_factory=list)
    newly_corroborated: list[str] = field(default_factory=list)
    seconds: float = 0.0


class Corroborator:
    """Evaluates the EventBuilder's in-memory events against sensor data.

    ``scores`` (event id -> roll-up score) is shared with the confidence scorer."""

    def __init__(self, store: SqliteStore, lake: Lake, metrics: Metrics | None = None,
                 config: CorroborationConfig | None = None, *,
                 scores: dict[str, float] | None = None,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self.lake = lake
        self.metrics = metrics or Metrics()
        self.cfg = config or CorroborationConfig.from_file()
        self.scores: dict[str, float] = scores if scores is not None else {}
        self.clock = clock
        self._evaluated: dict[str, tuple] = {}  # event id -> (updated_at, lat, lon)
        self._obs_seen: set[tuple[str, str]] = set()
        self._last_run: datetime | None = None
        self.passes = 0
        self.last: CorroborationResult | None = None

    def warm_start(self, now: datetime) -> None:
        self.scores.update(self.store.sensor_scores_since(
            now - timedelta(hours=self.cfg.reeval_window_h + 72)))

    def due(self, now: datetime) -> bool:
        return self._last_run is None or (
            (now - self._last_run).total_seconds() >= self.cfg.interval_s)

    # --------------------------------------------------------------- dirty set

    def _max_radius(self, category: str) -> float:
        return max((r.radius_km for r in self.cfg.categories.get(category, ())), default=0.0)

    def _new_data_cells(self, now: datetime) -> set[int] | None:
        """res-3 cells with positions/observations new since the last pass (None = first
        pass: everything is new)."""
        horizon = now - timedelta(hours=self.cfg.reeval_window_h + 24)
        rows = self.lake.query(
            "SELECT id, version, h3_3 FROM observations WHERE ts >= ?", [horizon],
            start=horizon)
        seen = {(r[0], r[1]) for r in rows}
        fresh = {r[2] for r in rows if (r[0], r[1]) not in self._obs_seen}
        first = self._last_run is None
        self._obs_seen = seen
        if first:
            return None
        since = self._last_run - timedelta(minutes=10)  # late lake flushes
        fresh |= {r[0] for r in self.lake.query(
            "SELECT DISTINCT h3_3 FROM positions WHERE ts >= ?", [since], start=since)}
        return fresh

    def select(self, events: dict[str, Any], now: datetime) -> list[Any]:
        recent = now - timedelta(hours=self.cfg.reeval_window_h)
        cands = [ev for ev in events.values()
                 if ev.updated_at >= recent and ev.category in self.cfg.categories
                 and ev.lat is not None and ev.lon is not None]
        new_cells = self._new_data_cells(now)
        # forget events that left the builder's index (bounded memory)
        self._evaluated = {k: v for k, v in self._evaluated.items() if k in events}
        out = []
        for ev in cands:
            key = (ev.updated_at, ev.lat, ev.lon)
            if new_cells is None or self._evaluated.get(ev.id) != key:
                out.append(ev)
                continue
            _, cells = search_cells(ev.lat, ev.lon, self._max_radius(ev.category) + 50, 0)
            if cells & new_cells:
                out.append(ev)
        return out

    # ------------------------------------------------------------------ run

    def maybe_run(self, events: dict[str, Any], now: datetime,
                  scorer: Callable[[Any, datetime], tuple[str, float]]
                  ) -> CorroborationResult | None:
        if not self.due(now):
            return None
        return self.run(events, now, scorer)

    def run(self, events: dict[str, Any], now: datetime,
            scorer: Callable[[Any, datetime], tuple[str, float]]) -> CorroborationResult:
        """Evaluate dirty events, persist evidence + roll-ups, update status/confidence of
        events whose score changed (mutates the EventStates; saves their rows)."""
        t0 = time.perf_counter()
        res = CorroborationResult()
        todo = self.select(events, now)
        reqs: list[_Req] = []
        evaluated: list[Any] = []
        for ev in todo:
            factor = self.cfg.precision_factor.get(ev.precision or "", 0.0)
            if factor <= 0:
                res.skipped["precision"] = res.skipped.get("precision", 0) + 1
                evaluated.append(ev)  # a coarse event drops any earlier evidence
                continue
            text = _WORDS.findall(f"{ev.title} {ev.summary}".casefold())
            for rule in self.cfg.categories.get(ev.category, ()):
                if not rule.applies(text):
                    continue
                r5 = self.cfg.res5_max_k if ev.precision in ("point", "city") else 0
                res_, cells = search_cells(ev.lat, ev.lon, rule.radius_km, r5)
                reqs.append(_Req(ev.id, ev.lat, ev.lon, ev.t_start, ev.t_end, rule, factor,
                                 res_, cells))
            evaluated.append(ev)
        rows = self.evaluate(reqs)
        res.evaluated = len(evaluated)
        self._persist(evaluated, rows, now, scorer, res)
        self._last_run = now
        res.seconds = time.perf_counter() - t0
        self.passes += 1
        self.last = res
        m = self.metrics
        for kind, n in res.new_rows.items():
            m.inc("corroborations_total", n, kind=kind)
        m.inc("events_sensor_corroborated_total", len(res.newly_corroborated))
        for reason, n in res.skipped.items():
            m.inc("corroboration_skipped_total", n, reason=reason)
        m.set("corroboration_pass_seconds", res.seconds)
        m.set("corroboration_events_evaluated", res.evaluated)
        return res

    def evaluate(self, reqs: list[_Req]) -> list[dict]:
        """Evidence rows for the given lookups (no persistence)."""
        by_kind: dict[str, list[_Req]] = defaultdict(list)
        for r in reqs:
            by_kind[r.rule.kind].append(r)
        rows: list[dict] = []
        if by_kind.get("fire"):
            rows += self._fires(by_kind["fire"])
        if by_kind.get("quake"):
            rows += self._quakes(by_kind["quake"])
        if by_kind.get("hazard"):
            rows += self._hazards(by_kind["hazard"])
        if by_kind.get("aircraft"):
            rows += self._aircraft(by_kind["aircraft"])
        sea = by_kind.get("ship", []) + by_kind.get("ais_gap", [])
        if sea:
            rows += self._ships(sea)
        # cap rows per (event, kind) and keep the strongest; one row per (event, kind, ref)
        best: dict[tuple[str, str, str], dict] = {}
        for r in rows:
            k = (r["event_id"], r["kind"], r["ref_id"])
            if k not in best or r["score"] > best[k]["score"]:
                best[k] = r
        grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for r in best.values():
            grouped[(r["event_id"], r["kind"])].append(r)
        out = []
        for g in grouped.values():
            g.sort(key=lambda r: (-r["score"], r["ref_id"]))
            out += g[: self.cfg.max_rows_per_kind]
        return out

    # ------------------------------------------------------------- evidence

    def _row(self, req: _Req, kind: str, ref: str, rule: str, d: float, ts: datetime,
             score: float, details: dict) -> dict:
        return {"event_id": req.event_id, "kind": kind, "ref_id": ref, "rule": rule,
                "distance_km": round(d, 2), "dt_minutes": req.dt_minutes(ts),
                "score": round(min(1.0, score) * req.rule.weight * req.factor, 4),
                "details": details}

    def _observations(self, reqs: list[_Req], obs_type: str) -> list[tuple]:
        idx = _CellIndex(reqs)
        lo, hi = idx.window()
        sql = (f"SELECT id, epoch_us(ts), lat, lon, value, attrs, h3_3, h3_5 FROM observations "
               f"WHERE type = ? AND ts >= ? AND ts <= ? AND {idx.where()}")
        out = []
        for r in self.lake.query(sql, [obs_type, lo, hi], start=lo, end=hi):
            ts = ts_from_us(r[1])
            try:
                attrs = json.loads(r[5]) if r[5] else {}
            except ValueError:
                attrs = {}
            for req, d in idx.match(r[6], r[7], r[2], r[3], ts):
                out.append((req, d, r[0], ts, r[2], r[3], r[4], attrs))
        return out

    def _fires(self, reqs: list[_Req]) -> list[dict]:
        conf_w = {str(k).casefold(): float(v) for k, v in
                  (self.cfg.scoring.get("fire_confidence") or {}).items()}
        rows = []
        for req, d, oid, ts, lat, lon, frp, attrs in self._observations(reqs, "fire"):
            conf = str(attrs.get("confidence") or "").casefold()
            cw = conf_w.get(conf, 0.5)
            frp_term = 0.5 if frp is None else min(1.0, 0.4 + 0.3 * math.log10(1 + max(0, frp)))
            score = cw * frp_term * req.proximity(d) * req.timing(ts)
            rows.append(self._row(req, "fire", oid, "firms_hotspot", d, ts, score, {
                "frp_mw": frp, "confidence": attrs.get("confidence"), "lat": lat, "lon": lon,
                "ts": iso(ts), "satellite": attrs.get("satellite")}))
        return rows

    def _quakes(self, reqs: list[_Req]) -> list[dict]:
        types = {str(k).casefold(): float(v) for k, v in
                 (self.cfg.scoring.get("seismic_types") or {}).items()}
        rows = []
        for req, d, oid, ts, lat, lon, mag, attrs in self._observations(reqs, "quake"):
            etype = str(attrs.get("event_type") or "earthquake").casefold()
            depth = attrs.get("depth_km")
            p = req.rule.params
            if "min_mag" in p:  # disaster: magnitude-driven
                if mag is None or mag < float(p["min_mag"]):
                    continue
                base, rule = min(1.0, 0.5 + (mag - float(p["min_mag"])) / 4), "quake_magnitude"
            elif etype in types:
                base, rule = types[etype], "seismic_explosion"
            elif (etype == "earthquake" and depth is not None and mag is not None
                  and float(depth) <= float(p.get("max_shallow_depth_km", 5))
                  and mag <= float(p.get("max_shallow_mag", 3.5))):
                base, rule = self.cfg.s("shallow_quake", 0.4), "shallow_quake"
            else:
                continue
            score = base * req.proximity(d) * req.timing(ts)
            rows.append(self._row(req, "quake", oid, rule, d, ts, score, {
                "magnitude": mag, "event_type": etype, "depth_km": depth, "lat": lat,
                "lon": lon, "ts": iso(ts), "place": attrs.get("place"),
                "url": attrs.get("url")}))
        return rows

    def _hazards(self, reqs: list[_Req]) -> list[dict]:
        alert_w = {str(k).casefold(): float(v) for k, v in
                   (self.cfg.scoring.get("hazard_alert") or {}).items()}
        rows = []
        for req, d, oid, ts, lat, lon, value, attrs in self._observations(reqs, "hazard"):
            hazard = str(attrs.get("hazard") or "other")
            types = req.rule.params.get("hazards")
            if types and hazard not in types:
                continue
            alert = str(attrs.get("alert_level") or "").casefold()
            base = alert_w.get(alert or str(attrs.get("source") or ""), 0.6)
            score = base * req.proximity(d) * req.timing(ts)
            rows.append(self._row(req, "hazard", oid, f"{attrs.get('source')}_{alert or 'alert'}",
                                  d, ts, score, {
                                      "source": attrs.get("source"), "hazard": hazard,
                                      "name": attrs.get("name"), "alert_level": alert or None,
                                      "severity_text": attrs.get("severity_text"),
                                      "value": value, "lat": lat, "lon": lon, "ts": iso(ts),
                                      "url": attrs.get("url")}))
        return rows

    def _positions(self, idx: _CellIndex, kind: str, pad: timedelta) -> list[tuple]:
        lo, hi = idx.window(pad)
        sql = (f"SELECT track_id, epoch_us(ts), lat, lon, speed_kts, h3_3, h3_5 FROM positions "
               f"WHERE kind = ? AND ts >= ? AND ts <= ? AND {idx.where()}")
        return self.lake.query(sql, [kind, lo, hi], start=lo, end=hi)

    def _aircraft(self, reqs: list[_Req]) -> list[dict]:
        idx = _CellIndex(reqs)
        # (req id, track) -> [closest distance, ts at closest, n positions]
        hits: dict[tuple[int, str], list] = {}
        for track, us, lat, lon, _spd, c3, c5 in self._positions(idx, "air", timedelta()):
            ts = ts_from_us(us)
            for req, d in idx.match(c3, c5, lat, lon, ts):
                h = hits.setdefault((id(req), track), [req, d, ts, 0])
                h[3] += 1
                if d < h[1]:
                    h[1], h[2] = d, ts
        info = self.store.get_tracks(sorted({t for _, t in hits}))
        hp_s, mil_s = self.cfg.s("aircraft_high_priority", 0.45), self.cfg.s("aircraft", 0.25)
        rows = []
        for (_, track), (req, d, ts, n) in hits.items():
            t = info.get(track) or {}
            if not t.get("is_mil"):
                continue
            hp = bool(t.get("high_priority"))
            score = (hp_s if hp else mil_s) * req.proximity(d) * req.timing(ts)
            rows.append(self._row(req, "aircraft", track, "mil_aircraft", d, ts, score, {
                "type": t.get("type_code"), "callsign": t.get("name"), "reg": t.get("reg"),
                "category": t.get("category"), "high_priority": hp, "positions": n,
                "closest_ts": iso(ts)}))
        return rows

    def _ships(self, reqs: list[_Req]) -> list[dict]:
        idx = _CellIndex(reqs)
        gap_reqs = [r for r in reqs if r.rule.kind == "ais_gap"]
        pad = timedelta(hours=24) if gap_reqs else timedelta()
        cand = self._positions(idx, "sea", pad)
        tracks = sorted({r[0] for r in cand})
        info = self.store.get_tracks(tracks)
        rows: list[dict] = []
        # presence / loitering: positions inside each ship request's window and radius
        per: dict[tuple[int, str], list] = defaultdict(list)
        for track, us, lat, lon, spd, c3, c5 in cand:
            ts = ts_from_us(us)
            for req, d in idx.match(c3, c5, lat, lon, ts):
                if req.rule.kind == "ship":
                    per[(id(req), track)].append((req, d, ts, lat, lon, spd))
        for (_, track), pts in per.items():
            req = pts[0][0]
            rows.append(self._ship_row(req, track, pts, info.get(track) or {}))
        if gap_reqs and tracks:
            rows += self._gaps(gap_reqs, tracks)
        return rows

    def _ship_row(self, req: _Req, track: str, pts: list, t: dict) -> dict:
        p = req.rule.params
        pts.sort(key=lambda x: x[2])
        d_min, ts_min = min((x[1], x[2]) for x in pts)
        span_h = (pts[-1][2] - pts[0][2]).total_seconds() / 3600
        clat = sum(x[3] for x in pts) / len(pts)
        clon = sum(x[4] for x in pts) / len(pts)
        spread = max(haversine_km(clat, clon, x[3], x[4]) for x in pts)
        speeds = [x[5] for x in pts if x[5] is not None]
        mean_spd = sum(speeds) / len(speeds) if speeds else 0.0
        loiter = (span_h >= float(p.get("loiter_min_h", 2))
                  and spread <= float(p.get("loiter_radius_km", 5))
                  and mean_spd <= float(p.get("loiter_max_kts", 3)))
        if loiter:
            base, rule = self.cfg.s("ship_loiter", 0.5), "ship_loiter"
        elif t.get("is_mil"):
            base, rule = self.cfg.s("ship_military", 0.4), "ship_military"
        else:
            base, rule = self.cfg.s("ship", 0.15), "ship_presence"
        score = base * req.proximity(d_min) * req.timing(ts_min)
        return self._row(req, "ship", track, rule, d_min, ts_min, score, {
            "name": t.get("name"), "ship_type": t.get("category"), "military": bool(
                t.get("is_mil")), "positions": len(pts), "span_h": round(span_h, 2),
            "spread_km": round(spread, 2), "mean_speed_kts": round(mean_spd, 1)})

    def _gaps(self, reqs: list[_Req], tracks: list[str]) -> list[dict]:
        """AIS dark gaps of candidate ships, using all their positions (not just those in
        the search cells, so leaving the area is not mistaken for a gap)."""
        lo = min(r.win_start for r in reqs) - timedelta(hours=24)
        hi = max(r.win_end for r in reqs) + timedelta(hours=24)
        series: dict[str, list[tuple]] = defaultdict(list)
        for i in range(0, len(tracks), 500):
            chunk = tracks[i: i + 500]
            marks = ",".join("?" * len(chunk))
            for track, us, lat, lon in self.lake.query(
                    f"SELECT track_id, epoch_us(ts), lat, lon FROM positions WHERE kind='sea' "
                    f"AND ts >= ? AND ts <= ? AND track_id IN ({marks})",
                    [lo, hi, *chunk], start=lo, end=hi):
                series[track].append((ts_from_us(us), lat, lon))
        rows = []
        for track, pts in series.items():
            pts.sort()
            for (t1, la1, lo1), (t2, la2, lo2) in pairwise(pts):
                gap_h = (t2 - t1).total_seconds() / 3600
                for req in reqs:
                    if gap_h < float(req.rule.params.get("min_gap_h", 2)):
                        continue
                    if t2 < req.win_start or t1 > req.win_end:
                        continue
                    d = min(haversine_km(req.lat, req.lon, la1, lo1),
                            haversine_km(req.lat, req.lon, la2, lo2))
                    if d > req.rule.radius_km:
                        continue
                    ref_ts = max(t1, min(t2, req.t_start))
                    score = (self.cfg.s("ais_gap", 0.6) * min(1.0, gap_h / 6)
                             * req.proximity(d) * req.timing(ref_ts))
                    rows.append(self._row(req, "ais_gap", f"{track}@{iso(t1)}", "ais_dark_gap",
                                          d, ref_ts, score, {
                                              "track_id": track, "gap_h": round(gap_h, 2),
                                              "gap_start": iso(t1), "gap_end": iso(t2),
                                              "last_lat": la1, "last_lon": lo1,
                                              "next_lat": la2, "next_lon": lo2}))
        return rows

    # -------------------------------------------------------------- persist

    def _persist(self, evaluated: list[Any], rows: list[dict], now: datetime,
                 scorer: Callable[[Any, datetime], tuple[str, float]],
                 res: CorroborationResult) -> None:
        if not evaluated:
            return
        ids = [ev.id for ev in evaluated]
        stored = self.store.corroboration_rows(ids)
        mine: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            mine[r["event_id"]].append(r)
        summaries, changed_rows = [], []
        for ev in evaluated:
            other = [r for r in stored.get(ev.id, []) if r["kind"] not in MANAGED_KINDS]
            score, kinds = rollup(mine.get(ev.id, []) + other, self.cfg.top_per_kind)
            evidence = len(mine.get(ev.id, [])) + len(other)
            corroborated = score >= self.cfg.threshold
            summaries.append({"event_id": ev.id, "score": score, "kinds": kinds,
                              "evidence": evidence, "corroborated": corroborated})
            old = self.scores.get(ev.id, 0.0)
            was = bool(ev.sensor_corroborated)
            self.scores[ev.id] = score
            if was != corroborated or abs(old - score) > 1e-4:
                ev.sensor_corroborated = corroborated
                old_status = ev.status
                ev.status, ev.confidence = scorer(ev, now)
                if ev.status != old_status:
                    ev.updated_at = now
                if corroborated and not was:
                    res.newly_corroborated.append(ev.id)
                res.changed.append(ev.id)
                changed_rows.append(ev.to_row())
        res.new_rows = self.store.save_corroborations(
            rows, replace_for={ev.id: set(MANAGED_KINDS) for ev in evaluated},
            summaries=summaries, events=changed_rows)
        for ev in evaluated:
            self._evaluated[ev.id] = (ev.updated_at, ev.lat, ev.lon)


def load_config(settings: Any) -> CorroborationConfig:
    return CorroborationConfig.from_file(Path(settings.config_dir) / "corroboration.yaml")

