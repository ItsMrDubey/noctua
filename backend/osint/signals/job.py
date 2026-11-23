"""Signals job (worker): notable signals -> sensor-derived events, the signal alert feed and
radiation baselines. Thresholds: ``config/signals.yaml`` (all optional).

Every ``interval_s`` (120 s) it reads the collectors' live snapshots (``signals/*.json``):

* ``squawk`` events — one per emergency episode (one flight squawking one code), category
  air_activity, confidence by code (7700 > 7600 > 7500: a hijack code is most often a
  mis-set transponder). Deduped by episode id, so re-running changes nothing.
* ``gnss_jam`` events — connected clusters of live GNSS cells at level ``high`` with at least
  ``min_aircraft`` aircraft and ``min_pct`` degraded, category cyber_outage. A cluster that
  continues within ``merge_gap_h`` of an existing event nearby extends it.
* ``outage`` events — IODA outage events passing the datasource / score thresholds
  (``osint.signals.outages.notable``), country precision, category cyber_outage.
* ``radiation`` events — stations whose latest reading is ``elevated`` / ``alert`` against
  their own baseline (median over ``baseline_days``, refreshed hourly from the lake into
  ``signals/radiation_baseline.json``), category disaster.

The alert feed (``signals/alerts.json``) collects the same signals plus newly airborne
high-priority military flights (from the ADS-B tracks, one alert per flight) and notable SWPC
space weather alerts (G2+ / S1+ / R2+), newest first, for the UI's alert center.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import h3
import yaml

from osint.core.geo import haversine_km
from osint.pipeline.cluster import CLUSTER_VERSION, new_ulid
from osint.signals.aviation import SEVERITY
from osint.signals.cache import SignalReader, write_signal
from osint.signals.outages import notable as outage_notable
from osint.signals.radiation import DEFAULTS as RAD_DEFAULTS
from osint.signals.radiation import assess
from osint.store.sqlite import iso

log = logging.getLogger(__name__)

DETECTORS = ("squawk", "gnss_jam", "outage", "radiation")
SQUAWK_CONFIDENCE = {"7700": 0.85, "7600": 0.7, "7500": 0.6}
DEFAULTS: dict[str, Any] = {
    "interval_s": 120.0, "initial_delay_s": 45.0,
    "squawk": {"enabled": True, "max_age_h": 24},
    "gnss_jam": {"enabled": True, "min_aircraft": 5, "min_pct": 25.0, "merge_gap_h": 6},
    "outage": {"enabled": True},
    "radiation": {"enabled": True, "baseline_days": 7, "baseline_refresh_min": 60,
                  "category": "disaster"},
    "alerts": {"keep_h": 48, "max": 300, "hp_flights": True, "hp_gap_h": 3,
               "hp_max_age_min": 10},
}


def load_config(path: Path) -> dict[str, Any]:
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if path.is_file():
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k] = {**cfg[k], **v}
            else:
                cfg[k] = v
    return cfg


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts), UTC)


def _place(lat: float, lon: float) -> str:
    return (f"{abs(lat):.2f}{'N' if lat >= 0 else 'S'} "
            f"{abs(lon):.2f}{'E' if lon >= 0 else 'W'}")


@dataclass
class Signal:
    """One notable signal, before de-duplication against stored events."""

    detector: str
    key: str
    category: str
    lat: float
    lon: float
    t_start: datetime
    t_end: datetime
    confidence: float
    title: str
    summary: str
    evidence: list[dict]
    details: dict = field(default_factory=dict)
    precision: str = "point"
    place_name: str | None = None
    country: str | None = None
    merge_gap_h: float = 0.0  # > 0: extend a nearby event of the detector instead


class SignalsJob:
    def __init__(self, store, lake, data_dir: Path, cfg: dict | None = None,
                 metrics=None, clock=time.time) -> None:
        self.store, self.lake = store, lake
        self.data_dir = Path(data_dir)
        self.cfg = cfg or load_config(Path("/nonexistent"))
        self.metrics = metrics
        self.clock = clock
        self.reader = SignalReader(self.data_dir)
        self._baseline_at = 0.0
        self._stop = asyncio.Event()
        self.passes = 0
        self.last: dict[str, Any] | None = None
        self.last_error: str | None = None
        state = store.get_cursor("signals_job") or {}
        self.alerts: list[dict] = list(state.get("alerts") or [])
        self.hp_seen: dict[str, float] = dict(state.get("hp_seen") or {})

    # ------------------------------------------------------------------ signals

    def squawk_signals(self, now: float) -> list[Signal]:
        c = self.cfg["squawk"]
        snap = self.reader.get("squawks") or {}
        out = []
        for ep in snap.get("items") or []:
            if ep.get("lat") is None or now - float(ep["last_seen"]) > float(
                    c["max_age_h"]) * 3600:
                continue
            code = str(ep["code"])
            who = ep.get("flight") or ep.get("reg") or ep["hex"].upper()
            kind = " ".join(x for x in (ep.get("type"), "military" if ep.get("mil") else "")
                            if x)
            title = f"Squawk {code} ({ep.get('label') or code}): {who}" + (
                f" ({kind})" if kind else "")
            alt = ep.get("alt_ft")
            summary = (f"{who} ({ep['hex'].upper()}{', ' + ep['reg'] if ep.get('reg') else ''}"
                       f"{', ' + ep['desc'] if ep.get('desc') else ''}) reported "
                       f"{'squawk ' + code if code.isdigit() else 'emergency status ' + code}"
                       f" ({ep.get('label') or code})"
                       + (f" at {int(alt):,} ft" if isinstance(alt, (int, float)) else "")
                       + ".")
            out.append(Signal(
                "squawk", f"squawk:{ep['id']}", "air_activity", float(ep["lat"]),
                float(ep["lon"]), _dt(ep["first_seen"]), _dt(ep["last_seen"]),
                SQUAWK_CONFIDENCE.get(code, 0.6), title, summary,
                [{"kind": "aircraft", "ref_id": f"air:{ep['hex']}", "rule": "squawk",
                  "distance_km": 0.0, "dt_minutes": 0.0,
                  "score": SQUAWK_CONFIDENCE.get(code, 0.6),
                  "details": {k: ep.get(k) for k in ("code", "label", "flight", "reg", "type",
                                                     "alt_ft", "squawk", "emergency")}}],
                {"hex": ep["hex"], "code": code, "flight": ep.get("flight"),
                 "episode": ep["id"]}))
        return out

    def gnss_jam_signals(self, now: float) -> list[Signal]:
        c = self.cfg["gnss_jam"]
        snap = self.reader.get("gnss") or {}
        if not snap or now - float(snap.get("updated") or 0) > 1800:
            return []
        hot = {x["h3"]: x for x in snap.get("cells") or []
               if x.get("level") == "high" and x["n"] >= int(c["min_aircraft"])
               and x["pct"] >= float(c["min_pct"])}
        regions = snap.get("regions") or []
        out = []
        seen: set[str] = set()
        for start in sorted(hot, key=lambda k: -hot[k]["pct"]):
            if start in seen:
                continue
            group, stack = [], [start]  # connected component of hot cells
            seen.add(start)
            while stack:
                cell = stack.pop()
                group.append(hot[cell])
                for nb in h3.grid_disk(cell, 1):
                    if nb in hot and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
            n = sum(x["n"] for x in group)
            bad = sum(x["bad"] for x in group)
            lat = sum(x["lat"] * x["n"] for x in group) / n
            lon = sum(x["lon"] * x["n"] for x in group) / n
            pct = 100.0 * bad / n
            region = min(regions, key=lambda r: haversine_km(lat, lon, r["lat"], r["lon"]),
                         default=None)
            named = region is not None and haversine_km(
                lat, lon, region["lat"], region["lon"]) <= region.get("dist_nm", 250) * 1.852
            where = region["name"] if named else _place(lat, lon)
            window = timedelta(minutes=float(snap.get("window_min") or 60))
            # the snapshot time rounded to 10 min: a persisting pattern extends its event at
            # most that often
            at = _dt(float(snap["updated"]) // 600 * 600)
            conf = min(0.95, 0.5 + 0.3 * min(1.0, (pct - float(c["min_pct"])) / 50)
                       + 0.15 * min(1.0, (bad - 2) / 10))
            out.append(Signal(
                "gnss_jam", f"gnss_jam:{h3.latlng_to_cell(lat, lon, 3)}", "cyber_outage", lat,
                lon, at - window, at, round(conf, 4),
                f"GNSS interference: {where}" + (f" ({_place(lat, lon)})" if named else ""),
                f"{bad} of {n} aircraft ({pct:.0f}%) reported degraded GPS navigation accuracy "
                f"(NIC/NACp below normal or GPS lost) in {len(group)} area cell(s) over "
                f"{int(window.total_seconds() // 60)} min: likely jamming.",
                [{"kind": "gnss_cell", "ref_id": x["h3"], "rule": "gnss_jam",
                  "distance_km": round(haversine_km(lat, lon, x["lat"], x["lon"]), 1),
                  "dt_minutes": 0.0, "score": round(x["pct"] / 100, 4),
                  "details": {"aircraft": x["n"], "degraded": x["bad"], "pct": x["pct"]}}
                 for x in group],
                {"aircraft": n, "degraded": bad, "pct": round(pct, 1), "cells": len(group),
                 "region": where}, place_name=where, merge_gap_h=float(c["merge_gap_h"])))
        return out

    def outage_signals(self, now: float) -> list[Signal]:
        snap = self.reader.get("outages") or {}
        out = []
        for e in snap.get("events") or []:
            if not outage_notable(e, self.cfg.get("outage")) or e.get("lat") is None:
                continue
            hours = (e["until"] - e["from"]) / 3600
            state = "ongoing" if e.get("ongoing") else "ended"
            out.append(Signal(
                "outage", f"outage:{e['cc']}:{e['datasource']}:{e['from']}", "cyber_outage",
                e["lat"], e["lon"], _dt(e["from"]), _dt(e["until"]),
                round(min(0.95, 0.4 + 0.55 * e["severity"]), 4),
                f"Internet outage: {e['name']}",
                f"IODA detected an internet connectivity drop in {e['name']} "
                f"({e['datasource']}, score {e['score']:,.0f}, {hours:.1f} h, {state}).",
                [{"kind": "outage", "ref_id": f"{e['cc']}:{e['datasource']}:{e['from']}",
                  "rule": "ioda", "distance_km": 0.0, "dt_minutes": 0.0,
                  "score": e["severity"], "details": {k: e.get(k) for k in (
                      "datasource", "method", "score", "from", "until", "ongoing")}}],
                {"cc": e["cc"], "datasource": e["datasource"], "score": e["score"]},
                precision="country", place_name=e["name"], country=e["cc"]))
        return out

    def radiation_baselines(self, now: float) -> dict[str, list]:
        c = self.cfg["radiation"]
        cached = self.reader.get("radiation_baseline") or {}
        if now - self._baseline_at < float(c["baseline_refresh_min"]) * 60 and cached:
            return cached.get("stations") or {}
        end = _dt(now) - timedelta(hours=2)
        start = end - timedelta(days=float(c["baseline_days"]))
        rows = self.lake.query(
            "SELECT id, median(value), count(*) FROM (SELECT id, value FROM observations "
            "WHERE type = 'radiation' AND ts >= ? AND ts < ? QUALIFY row_number() OVER "
            "(PARTITION BY id, version ORDER BY ts) = 1) GROUP BY id",
            [start, end], start=start, end=end)
        stations = {r[0].removeprefix("rad:"): [round(float(r[1]), 4), int(r[2])]
                    for r in rows if r[1] is not None}
        thresholds = {k: v for k, v in {**RAD_DEFAULTS, **c}.items() if k in RAD_DEFAULTS}
        write_signal(self.data_dir, "radiation_baseline", {
            "updated": now, "thresholds": thresholds, "stations": stations})
        self._baseline_at = now
        return stations

    def radiation_signals(self, now: float) -> list[Signal]:
        c = {**RAD_DEFAULTS, **self.cfg["radiation"]}
        base = self.radiation_baselines(now)
        snap = self.reader.get("radiation") or {}
        out = []
        for s in snap.get("stations") or []:
            status = assess(float(s["value"]), base.get(s["id"]), c)
            if status not in ("elevated", "alert"):
                continue
            b = base.get(s["id"])
            ts = datetime.fromisoformat(s["ts"])
            ratio = f", {s['value'] / b[0]:.1f}x its {c['baseline_days']}-day median " \
                f"{b[0]:.3f}" if b and b[0] else ""
            out.append(Signal(
                "radiation", f"radiation:{s['id']}:{ts.strftime('%Y%m%d')}",
                str(c["category"]), s["lat"], s["lon"], ts, ts,
                0.8 if status == "alert" else 0.55,
                f"Radiation {'alert' if status == 'alert' else 'elevated'}: "
                f"{s.get('name') or s['id']}",
                f"{s['source'].upper()} station {s.get('name') or s['id']} measured "
                f"{s['value']:.3f} µSv/h{ratio}. Rain can raise readings briefly; check the "
                f"neighbouring stations.",
                [{"kind": "radiation", "ref_id": s["id"], "rule": status, "distance_km": 0.0,
                  "dt_minutes": 0.0, "score": 0.8 if status == "alert" else 0.55,
                  "details": {"value": s["value"], "baseline": b, "source": s["source"]}}],
                {"station": s["id"], "value": s["value"], "baseline": b, "status": status},
                place_name=s.get("name"), country=s.get("country") or None))
        return out

    # ------------------------------------------------------------------ emit

    def emit(self, sig: Signal, now_dt: datetime) -> str | None:
        """Create or extend the event for ``sig``: 'created' | 'updated' | None (unchanged)."""
        cell = h3.latlng_to_cell(sig.lat, sig.lon, 3)
        existing = self.store.get_sensor_event_by_key(sig.key)
        if existing is None and sig.merge_gap_h > 0:
            existing = self.store.find_sensor_event(
                sig.detector, list(h3.grid_disk(cell, 1)),
                sig.t_start - timedelta(hours=sig.merge_gap_h))
        old = self.store.get_event(existing["event_id"]) if existing else None
        eid = existing["event_id"] if existing else new_ulid(now_dt)
        t_start, t_end, conf, version, first_seen = sig.t_start, sig.t_end, sig.confidence, 1, \
            now_dt
        if old is not None:
            t_start = min(t_start, datetime.fromisoformat(old["t_start"]))
            t_end = max(t_end, datetime.fromisoformat(old["t_end"]))
            conf = max(conf, float(old["confidence"]))
            known = {(r["kind"], r["ref_id"]) for r in self.store.list_corroborations(eid)}
            fresh = [e for e in sig.evidence if (e["kind"], e["ref_id"]) not in known]
            if (iso(t_start) == old["t_start"] and iso(t_end) == old["t_end"]
                    and conf == old["confidence"] and not fresh
                    and sig.summary == old.get("summary")):
                return None
            version = int(old["version"]) + 1
            first_seen = datetime.fromisoformat(old["first_seen"])
            n_evidence = len(known | {(e["kind"], e["ref_id"]) for e in sig.evidence})
        else:
            n_evidence = len(sig.evidence)
        lat, lon = (sig.lat, sig.lon) if old is None else (old["lat"], old["lon"])
        row = {
            "id": eid, "category": sig.category, "subcategory": sig.detector,
            "cameo_root": None, "title": sig.title if old is None else old["title"],
            "summary": sig.summary, "actors": [], "lat": lat, "lon": lon,
            "precision": sig.precision,
            "place_name": (sig.place_name or _place(lat, lon)) if old is None
            else old["place_name"],
            "country": sig.country, "geo_confidence": 1.0 if sig.precision == "point" else 0.5,
            "h3_3": h3.latlng_to_cell(lat, lon, 3), "h3_5": h3.latlng_to_cell(lat, lon, 5),
            "h3_7": h3.latlng_to_cell(lat, lon, 7),
            "t_start": iso(t_start), "t_end": iso(t_end), "status": "sensor-corroborated",
            "confidence": round(conf, 4), "version": version, "mention_count": n_evidence,
            "source_diversity": 0, "gdelt_ids": [], "first_seen": iso(first_seen),
            "updated_at": iso(now_dt),
            "cluster_state": {"sensor": True, "detector": sig.detector,
                              "sensor_corroborated": True, "v": CLUSTER_VERSION},
        }
        self.store.save_sensor_event(
            row, {"detector": sig.detector,
                  "dedupe_key": existing["dedupe_key"] if existing else sig.key,
                  "cell": existing["cell"] if existing else cell,
                  "details": {**sig.details, "detector_version": 1}},
            [{**e, "event_id": eid} for e in sig.evidence],
            {"event_id": eid, "score": round(conf, 4),
             "kinds": sorted({e["kind"] for e in sig.evidence}), "evidence": n_evidence,
             "corroborated": True})
        return "created" if old is None else "updated"

    # ------------------------------------------------------------------ alerts

    def _alert(self, aid: str, ts: float, sev: str, kind: str, title: str, sub: str = "",
               lat: float | None = None, lon: float | None = None, **extra) -> None:
        if any(a["id"] == aid for a in self.alerts):
            return
        self.alerts.append({"id": aid, "ts": ts, "sev": sev, "kind": kind, "title": title,
                            "sub": sub, "lat": lat, "lon": lon, **extra})

    def hp_flight_alerts(self, now: float) -> None:
        c = self.cfg["alerts"]
        since = _dt(now) - timedelta(minutes=float(c["hp_max_age_min"]))
        rows = self.store.recent_tracks("air", since, high_priority=True)
        gap = float(c["hp_gap_h"]) * 3600
        live = {}
        for t in rows:
            flight = f"{t['track_id']}:{(t.get('name') or '').strip()}"
            last = self.hp_seen.get(flight)
            if last is None or now - last > gap:
                live[flight] = t
            self.hp_seen[flight] = now
        for flight, t in live.items():
            who = t.get("name") or t.get("reg") or t["track_id"]
            self._alert(f"hp:{flight}:{int(now)}", now, "high", "aircraft",
                        f"High-priority {t.get('type_code') or 'aircraft'} · {who}",
                        " · ".join(x for x in (t.get("operator"), t.get("category")) if x),
                        track_id=t["track_id"])
        self.hp_seen = {k: v for k, v in self.hp_seen.items() if now - v <= gap}

    def signal_alerts(self, sigs: list[Signal], now: float) -> None:
        for s in sigs:
            if s.detector == "squawk":
                code = s.details["code"]
                sev = SEVERITY.get(code, "warn")
            elif s.detector == "radiation":
                sev = "high" if s.details.get("status") == "alert" else "warn"
            else:
                sev = "warn"
            self._alert(s.key, now, sev, s.detector, s.title, s.summary[:160], s.lat, s.lon,
                        at=s.t_start.timestamp())
        sw = self.reader.get("spaceweather") or {}
        for a in sw.get("alerts") or []:
            if a.get("notable") and now - float(a["issued"]) <= 6 * 3600:
                sc = " ".join(f"{k}{v}" for k, v in sorted((a.get("scales") or {}).items()))
                self._alert(f"swpc:{a['id']}", a["issued"], "warn", "space",
                            f"Space weather {a['kind'].lower()}: {a['headline']}"[:140],
                            ("NOAA scale " + sc) if sc else "NOAA SWPC")

    # ------------------------------------------------------------------ pass

    def run_once(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        now = self.clock()
        now_dt = _dt(now)
        out: dict[str, Any] = {"created": {}, "updated": {}, "signals": {}}
        sigs: list[Signal] = []
        for name in DETECTORS:
            if not self.cfg[name].get("enabled", True):
                continue
            try:
                found = getattr(self, f"{name}_signals")(now)
            except Exception:
                log.exception("signals: %s detector failed", name)
                found = []
            out["signals"][name] = len(found)
            for s in found:
                try:
                    kind = self.emit(s, now_dt)
                except Exception:
                    log.exception("signals: emitting %s failed", s.key)
                    continue
                if kind:
                    out[kind][name] = out[kind].get(name, 0) + 1
            sigs += found
        if self.metrics is not None:
            for name, n in out["created"].items():
                self.metrics.inc("signal_events_created_total", n, detector=name)
        if self.cfg["alerts"].get("hp_flights", True):
            try:
                self.hp_flight_alerts(now)
            except Exception:
                log.exception("signals: high-priority flight alerts failed")
        self.signal_alerts(sigs, now)
        c = self.cfg["alerts"]
        keep = now - float(c["keep_h"]) * 3600
        self.alerts = sorted((a for a in self.alerts if a["ts"] >= keep),
                             key=lambda a: -a["ts"])[: int(c["max"])]
        write_signal(self.data_dir, "alerts", {"updated": now, "items": self.alerts})
        self.store.set_cursor("signals_job", {"alerts": self.alerts, "hp_seen": self.hp_seen})
        out["seconds"] = round(time.perf_counter() - t0, 3)
        self.passes += 1
        self.last = out
        return out

    async def run(self) -> None:
        self._stop.clear()
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=float(self.cfg["initial_delay_s"]))
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
                log.exception("signals pass failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=float(self.cfg["interval_s"]))
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


def start(runtime) -> asyncio.Task:
    """Create ``runtime.signals_job`` and start its loop (one-line wiring from Runtime._boot)."""
    settings = runtime.settings
    job = SignalsJob(runtime.store, runtime.lake, settings.data_dir,
                     load_config(Path(settings.config_dir) / "signals.yaml"), runtime.metrics)
    runtime.signals_job = job
    task = asyncio.create_task(job.run(), name="signals")
    runtime._tasks.append(task)
    return task

