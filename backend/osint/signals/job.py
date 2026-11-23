"""Signals job (worker): notable signals -> sensor-derived events, the signal alert feed and
radiation baselines. Thresholds: ``config/signals.yaml`` (all optional).

Every ``interval_s`` (120 s) it reads the collectors' live snapshots (``signals/*.json``):

* ``squawk`` events — one per *persistent* emergency episode (one flight squawking one code
  in at least ``min_reports`` reports spanning ``min_span_s``; osint.signals.aviation),
  category air_activity, confidence by code (7700 > 7600 > 7500: a hijack code is most often
  a mis-set transponder). A one-report blip only becomes a low-severity "transient squawk"
  alert. Deduped by episode id, so re-running changes nothing.
* ``gnss_jam`` events — connected clusters of live GNSS cells at level ``high`` with at least
  ``min_aircraft`` aircraft and ``min_pct`` degraded, category cyber_outage. A cluster that
  continues within ``merge_gap_h`` of an existing event nearby extends it. The event keeps
  its detection reading, peak, latest reading (every pass, also once the cells are quiet),
  a 10-min series and a lifecycle (active / subsiding / ended); confidence and wording
  ("possible" / "likely") come from the Wilson interval of the degraded share, the number
  of aircraft and the persistence across 60-min windows (osint.signals.gnss.assess).
* ``outage`` events — IODA outage events passing the datasource / score thresholds
  (``osint.signals.outages.notable``), country precision, category cyber_outage.
* ``radiation`` events — stations whose latest reading is ``elevated`` / ``alert`` against
  their own baseline (median over ``baseline_days``, refreshed hourly from the lake into
  ``signals/radiation_baseline.json``), category disaster.

The alert feed (``signals/alerts.json``) collects the same signals plus newly airborne
high-priority military flights (from the ADS-B tracks, one alert per flight) and notable SWPC
space weather alerts (G2+ / S1+ / R2+), newest first, for the UI's alert center.

Every event has status ``sensor-detected`` (osint.core.sensor_status): the generating
sensor's readings are stored as evidence rows but never count as corroboration.
``migrate()`` moves events written before that rule (idempotent, under the event rebuild
lock) and drops one-report squawk events.
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
from osint.core.sensor_status import SENSOR_DETECTED
from osint.pipeline.cluster import CLUSTER_VERSION, new_ulid
from osint.signals import gnss as gnss_mod
from osint.signals.aviation import SEVERITY, persistent, reports
from osint.signals.cache import SignalReader, write_signal
from osint.signals.outages import DEFAULT_WEIGHTS, datasource_label
from osint.signals.outages import notable as outage_notable
from osint.signals.radiation import DEFAULTS as RAD_DEFAULTS
from osint.signals.radiation import assess
from osint.store.sqlite import iso

log = logging.getLogger(__name__)

DETECTORS = ("squawk", "gnss_jam", "outage", "radiation")
SQUAWK_CONFIDENCE = {"7700": 0.85, "7600": 0.7, "7500": 0.6}
DEFAULTS: dict[str, Any] = {
    "interval_s": 120.0, "initial_delay_s": 45.0,
    "squawk": {"enabled": True, "max_age_h": 24, "min_reports": 2, "min_span_s": 20.0},
    "gnss_jam": {"enabled": True, "min_aircraft": 5, "min_pct": 25.0, "min_bad": 3,
                 "merge_gap_h": 6, "track_h": 12, "medium_pct": 2.0,
                 **gnss_mod.ASSESS_DEFAULTS},
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


def _dur(seconds: float) -> str:
    s = max(0, round(seconds))
    if s < 120:
        return f"{s} s"
    m = s // 60
    return f"{m} min" if m < 120 else f"{m // 60} h {m % 60:02d} min"


def _hm(t: datetime | float | str) -> str:
    """'23 Nov 04:50Z' for a datetime / unix seconds / ISO string."""
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    elif not isinstance(t, datetime):
        t = _dt(t)
    return t.astimezone(UTC).strftime("%d %b %H:%MZ")


# ---------------------------------------------------------------------- GNSS lifecycle

DETECTOR_VERSION = 2  # 2: sensor-detected status, GNSS lifecycle details
MIGRATE_LOCK_WAIT_S = 15.0  # an event builder pass holds the rebuild lock for seconds


def _gnss_hot(x: dict, c: dict) -> bool:
    """A live cell that meets the event criteria (level high, enough aircraft / share)."""
    return (x.get("level") == "high" and x["n"] >= int(c["min_aircraft"])
            and x["pct"] >= float(c["min_pct"]))


def count_windows(times: list[str], window_min: float = 60) -> int:
    """Non-overlapping ``window_min`` windows among the (hot) reading times."""
    n, last = 0, None
    for t in sorted(datetime.fromisoformat(x) for x in times):
        if last is None or t - last >= timedelta(minutes=window_min):
            n, last = n + 1, t
    return n


def _push_series(d: dict, reading: dict, keep: int = 288) -> None:
    series = [s for s in d.get("series") or [] if s[0] != reading["t"]]
    series.append([reading["t"], reading.get("n") or 0, reading.get("bad") or 0,
                   bool(reading.get("hot"))])
    d["series"] = sorted(series)[-keep:]


def _reassess(d: dict, c: dict) -> None:
    pk = d.get("peak") or d["detection"]
    w = count_windows(d.get("hot") or [], float(d["detection"].get("window_min") or 60))
    d["assessment"] = gnss_mod.assess(int(pk["n"]), int(pk["bad"]), max(1, w), c)


def merge_gnss(old: dict | None, new: dict, c: dict) -> dict:
    """Fold one hot detection (``new`` = Signal.details with ``reading`` / ``ac``) into
    the stored details: detection (first), peak (best Wilson lower bound), hot reading
    times, cells, latest reading, series, assessment, lifecycle 'active'."""
    d = {k: v for k, v in (old or {}).items()}
    r = {**new["reading"], "hot": True}
    d.setdefault("detection", r)
    pk = d.get("peak")
    if pk is None or (r["wilson_lo"], r["n"]) > (pk.get("wilson_lo") or 0, pk.get("n") or 0):
        d["peak"] = r
    d["hot"] = sorted(set(d.get("hot") or []) | {r["t"]})[-288:]
    d["cells"] = sorted(set(d.get("cells") or []) | set(r["cells"]))
    if new.get("ac"):
        d["ac"], d["ac_t"] = new["ac"], r["t"]
    d["latest"] = r
    d["lifecycle"] = "active"
    d.pop("ended_at", None)
    d["region"] = new.get("region") or d.get("region")
    d["detector_version"] = DETECTOR_VERSION
    _push_series(d, r)
    _reassess(d, c)
    return d


def gnss_reading(d: dict, cells: list[dict], t: datetime, c: dict) -> dict:
    """Record the current (not hot) reading of the event's cells at ``t`` and derive the
    lifecycle: subsiding while some aircraft are still degraded (> medium_pct) or the last
    hot reading is within one window, else ended."""
    d = dict(d)
    n = sum(x["n"] for x in cells)
    bad = sum(x["bad"] for x in cells)
    r = {"t": iso(t), "n": n, "bad": bad, "pct": round(100.0 * bad / n, 1) if n else None,
         "cells": sorted(x["h3"] for x in cells), "hot": False}
    if any(_gnss_hot(x, c) for x in cells):
        r["hot"] = True  # hot again but below the cluster minimum: keep it active
    last_hot = max(datetime.fromisoformat(x) for x in d.get("hot") or [d["detection"]["t"]])
    window = timedelta(minutes=float(d["detection"].get("window_min") or 60))
    still = n and bad >= 1 and 100.0 * bad / n > float(c.get("medium_pct", 2.0))
    d["lifecycle"] = "active" if r["hot"] else "subsiding" if (
        still or t - last_hot < window) else "ended"
    if d["lifecycle"] == "ended":
        d["ended_at"] = iso(last_hot)
    d["latest"] = r
    _push_series(d, r)
    _reassess(d, c)
    return d


def legacy_gnss_details(sensor: dict | None, event: dict, c: dict,
                        cells: list[str] | None = None) -> dict:
    """Details of a GNSS event written before lifecycle tracking. Known: the first
    detection time (event t_start + one window), the last hot reading (aircraft / degraded
    at the sensor row's t_end) and the cells that were ever hot (evidence rows); the
    reading at the first detection was not kept."""
    old = (sensor or {}).get("details") or {}
    t_end = datetime.fromisoformat((sensor or {}).get("t_end") or event["t_end"])
    t_first = min(t_end, datetime.fromisoformat(event["t_start"]) + timedelta(minutes=60))
    n, bad = int(old.get("aircraft") or 0), int(old.get("degraded") or 0)
    lo, hi = gnss_mod.wilson(bad, n)
    cells = sorted(set(cells or []) | {(sensor or {}).get("cell") or event.get("h3_3")})
    last = {"t": iso(t_end), "window_min": 60, "n": n, "bad": bad,
            "pct": round(100.0 * bad / n, 1) if n else None, "cells": cells,
            "wilson_lo": round(100 * lo, 1), "wilson_hi": round(100 * hi, 1), "hot": True,
            "legacy": True}
    first = last if t_first == t_end else {
        "t": iso(t_first), "window_min": 60, "n": None, "bad": None, "pct": None,
        "cells": cells, "hot": True, "legacy": True, "unrecorded": True}
    d = {"detection": first, "peak": last, "latest": last,
         "hot": sorted({first["t"], last["t"]}), "cells": cells,
         "region": old.get("region"), "lifecycle": "active",
         "detector_version": DETECTOR_VERSION}
    _push_series(d, last)
    _reassess(d, c)
    return d


def gnss_summary(d: dict) -> str:
    """Event summary from the stored details: every number with its time window."""
    a, det = d["assessment"], d["detection"]
    pk, la = d.get("peak") or det, d.get("latest") or det
    w = int(det.get("window_min") or 60)
    t0 = datetime.fromisoformat(det["t"])

    def reading(r: dict) -> str:
        if r.get("unrecorded"):
            return "reading not recorded (stored before readings were kept)"
        if not r.get("n"):
            return "no aircraft reporting in the cell(s)"
        return f"{r['bad']} of {r['n']} aircraft ({r['pct']:.0f}%) degraded"

    out = [f"{a['wording'].capitalize()} GNSS interference (ADS-B NIC/NACp, single sensor)."]
    ci = "" if det.get("unrecorded") else (
        f", 95% interval {det.get('wilson_lo', 0):.0f}-{det.get('wilson_hi', 100):.0f}%")
    out.append(f"At detection {_hm(t0)} "
               f"({w}-min window from {(t0 - timedelta(minutes=w)).strftime('%H:%M')}Z): "
               f"{reading(det)} in {len(det.get('cells') or [])} cell(s){ci}.")
    if pk["t"] != det["t"]:
        out.append(f"{'Last hot reading' if pk.get('legacy') else 'Peak'} {_hm(pk['t'])}: "
                   f"{reading(pk)}, 95% interval {pk.get('wilson_lo', 0):.0f}-"
                   f"{pk.get('wilson_hi', 100):.0f}%.")
    if la["t"] != det["t"]:
        out.append(f"Now {_hm(la['t'])}: {reading(la)} - {d.get('lifecycle')}.")
    out.append(f"Seen degraded in {a['windows']} non-overlapping {w}-min window(s)."
               + (f" Not 'likely': {'; '.join(a['limits'])}." if a["wording"] != "likely"
                  else ""))
    return " ".join(out)


def migrate_events(store, cfg: dict, now: datetime) -> dict[str, int]:
    """Body of SignalsJob.migrate (caller holds the rebuild lock). Idempotent."""
    out = {"status": store.migrate_sensor_statuses(SENSOR_DETECTED), "squawk_removed": 0,
           "gnss_reassessed": 0}
    blips = []
    for row in store.recent_sensor_events("squawk", datetime(1970, 1, 1, tzinfo=UTC)):
        ev = store.get_event(row["event_id"])
        if ev is None:
            continue
        rep = (row.get("details") or {}).get("reports")
        span = (datetime.fromisoformat(ev["t_end"])
                - datetime.fromisoformat(ev["t_start"])).total_seconds()
        if (rep if isinstance(rep, int) else 2 if span >= 1 else 1) < int(
                cfg["squawk"]["min_reports"]) or span < float(cfg["squawk"]["min_span_s"]):
            blips.append(row["event_id"])
    if blips:
        out["squawk_removed"] = store.delete_events(blips, origin="sensor")
    for row in store.recent_sensor_events("gnss_jam", datetime(1970, 1, 1, tzinfo=UTC)):
        if (row.get("details") or {}).get("detection"):
            continue
        ev = store.get_event(row["event_id"])
        if ev is None:
            continue
        d = legacy_gnss_details(row, ev, cfg["gnss_jam"], [
            r["ref_id"] for r in store.list_corroborations(row["event_id"])
            if r["kind"] == "gnss_cell"])
        if store.update_sensor_event(row["event_id"], d, now=now, summary=gnss_summary(d),
                                     confidence=d["assessment"]["confidence"],
                                     status=SENSOR_DETECTED):
            out["gnss_reassessed"] += 1
    return out


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
        self._migrated = False
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
        self.transient: list[dict] = []  # one-report squawk episodes of the last pass

    # ------------------------------------------------------------------ signals

    def squawk_signals(self, now: float) -> list[Signal]:
        c = self.cfg["squawk"]
        snap = self.reader.get("squawks") or {}
        out = []
        self.transient = []
        for ep in snap.get("items") or []:
            if ep.get("lat") is None or now - float(ep["last_seen"]) > float(
                    c["max_age_h"]) * 3600:
                continue
            if not persistent(ep, c):
                # a one-off blip (crew dialling through the code, a mis-set transponder):
                # kept as a low-severity alert once it is over, never an event
                if not ep.get("active"):
                    self.transient.append(ep)
                continue
            code = str(ep["code"])
            who = ep.get("flight") or ep.get("reg") or ep["hex"].upper()
            kind = " ".join(x for x in (ep.get("type"), "military" if ep.get("mil") else "")
                            if x)
            title = f"Squawk {code} ({ep.get('label') or code}): {who}" + (
                f" ({kind})" if kind else "")
            alt = ep.get("alt_ft")
            n_rep = reports(ep)
            span = float(ep["last_seen"]) - float(ep["first_seen"])
            summary = (f"{who} ({ep['hex'].upper()}{', ' + ep['reg'] if ep.get('reg') else ''}"
                       f"{', ' + ep['desc'] if ep.get('desc') else ''}) reported "
                       f"{'squawk ' + code if code.isdigit() else 'emergency status ' + code}"
                       f" ({ep.get('label') or code})"
                       + (f" at {int(alt):,} ft" if isinstance(alt, (int, float)) else "")
                       + f" in {n_rep} reports over {_dur(span)}"
                       + (", still set" if ep.get("active") else ", no longer reported")
                       + ".")
            out.append(Signal(
                "squawk", f"squawk:{ep['id']}", "air_activity", float(ep["lat"]),
                float(ep["lon"]), _dt(ep["first_seen"]), _dt(ep["last_seen"]),
                SQUAWK_CONFIDENCE.get(code, 0.6), title, summary,
                [{"kind": "aircraft", "ref_id": f"air:{ep['hex']}", "rule": "squawk",
                  "distance_km": 0.0, "dt_minutes": 0.0,
                  "score": SQUAWK_CONFIDENCE.get(code, 0.6),
                  "details": {k: ep.get(k) for k in ("code", "label", "flight", "reg", "type",
                                                     "alt_ft", "squawk", "emergency")}
                  | {"ts": iso(_dt(ep["first_seen"])), "reports": n_rep}}],
                {"hex": ep["hex"], "code": code, "flight": ep.get("flight"),
                 "episode": ep["id"], "reports": n_rep, "reg": ep.get("reg"),
                 "type": ep.get("type"), "first_seen": iso(_dt(ep["first_seen"])),
                 "last_seen": iso(_dt(ep["last_seen"])), "duration_s": round(span),
                 "squawk_now": ep.get("squawk"), "emergency": ep.get("emergency"),
                 "alt_ft": alt, "gs": ep.get("gs"),
                 "lifecycle": "active" if ep.get("active") else "ended"}))
        return out

    def gnss_jam_signals(self, now: float) -> list[Signal]:
        c = self.cfg["gnss_jam"]
        snap = self.reader.get("gnss") or {}
        if not snap or now - float(snap.get("updated") or 0) > 1800:
            return []
        hot = {x["h3"]: x for x in snap.get("cells") or [] if _gnss_hot(x, c)}
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
            if bad < int(c["min_bad"]):
                continue
            lat = sum(x["lat"] * x["n"] for x in group) / n
            lon = sum(x["lon"] * x["n"] for x in group) / n
            pct = 100.0 * bad / n
            region = min(regions, key=lambda r: haversine_km(lat, lon, r["lat"], r["lon"]),
                         default=None)
            named = region is not None and haversine_km(
                lat, lon, region["lat"], region["lon"]) <= region.get("dist_nm", 250) * 1.852
            where = region["name"] if named else _place(lat, lon)
            window_min = int(float(snap.get("window_min") or 60))
            # the snapshot time rounded to 10 min: a persisting pattern extends its event at
            # most that often
            at = _dt(float(snap["updated"]) // 600 * 600)
            a = gnss_mod.assess(n, bad, 1, c)
            ac = [r | {"cell": x["h3"]} for x in group for r in x.get("ac") or []
                  if r.get("bad")][:40]
            reading = {"t": iso(at), "window_min": window_min, "n": n, "bad": bad,
                       "pct": round(pct, 1), "cells": sorted(x["h3"] for x in group),
                       "wilson_lo": a["wilson_lo"], "wilson_hi": a["wilson_hi"]}
            out.append(Signal(
                "gnss_jam", f"gnss_jam:{h3.latlng_to_cell(lat, lon, 3)}", "cyber_outage", lat,
                lon, at - timedelta(minutes=window_min), at, a["confidence"],
                f"GNSS interference: {where}" + (f" ({_place(lat, lon)})" if named else ""),
                "", [{"kind": "gnss_cell", "ref_id": x["h3"], "rule": "gnss_jam",
                      "distance_km": round(haversine_km(lat, lon, x["lat"], x["lon"]), 1),
                      "dt_minutes": 0.0, "score": round(x["pct"] / 100, 4),
                      "details": {"aircraft": x["n"], "degraded": x["bad"], "pct": x["pct"],
                                  "ts": iso(at), "window_min": window_min,
                                  "lat": x["lat"], "lon": x["lon"]}}
                     for x in group],
                {"aircraft": n, "degraded": bad, "pct": round(pct, 1), "cells": len(group),
                 "region": where, "reading": reading, "ac": ac},
                place_name=where, merge_gap_h=float(c["merge_gap_h"])))
        return out

    def outage_signals(self, now: float) -> list[Signal]:
        snap = self.reader.get("outages") or {}
        out = []
        for e in snap.get("events") or []:
            if not outage_notable(e, self.cfg.get("outage")) or e.get("lat") is None:
                continue
            hours = (e["until"] - e["from"]) / 3600
            state = "ongoing" if e.get("ongoing") else "ended"
            label = datasource_label(e["datasource"])
            out.append(Signal(
                "outage", f"outage:{e['cc']}:{e['datasource']}:{e['from']}", "cyber_outage",
                e["lat"], e["lon"], _dt(e["from"]), _dt(e["until"]),
                round(min(0.95, 0.4 + 0.55 * e["severity"]), 4),
                f"Internet outage: {e['name']}",
                f"IODA detected an internet connectivity drop in {e['name']} "
                f"({label}, score {e['score']:,.0f}, since {_hm(e['from'])}, {hours:.1f} h, "
                f"{state}).",
                [{"kind": "outage", "ref_id": f"{e['cc']}:{e['datasource']}:{e['from']}",
                  "rule": "ioda", "distance_km": 0.0, "dt_minutes": 0.0,
                  "score": e["severity"], "details": {k: e.get(k) for k in (
                      "datasource", "method", "score", "from", "until", "ongoing")}
                  | {"ts": iso(_dt(e["from"])), "until_iso": iso(_dt(e["until"])),
                     "weight": e.get("weight", DEFAULT_WEIGHTS.get(e["datasource"]))}}],
                {"cc": e["cc"], "datasource": e["datasource"], "score": e["score"],
                 "weight": e.get("weight"), "severity": e.get("severity"),
                 "from": iso(_dt(e["from"])), "until": iso(_dt(e["until"])),
                 "lifecycle": "active" if e.get("ongoing") else "ended",
                 "datasource_label": label},
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
                  "details": {"value": s["value"], "baseline": b, "source": s["source"],
                              "ts": iso(ts)}}],
                {"station": s["id"], "value": s["value"], "baseline": b, "status": status,
                 "ts": iso(ts), "source": s["source"], "unit": s.get("unit") or "µSv/h"},
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
        lat, lon = (sig.lat, sig.lon) if old is None else (old["lat"], old["lon"])
        details = {**sig.details, "detector_version": DETECTOR_VERSION}
        summary, conf = sig.summary, sig.confidence
        if sig.detector == "gnss_jam":
            prior = (existing or {}).get("details") or {}
            if old is not None and not prior.get("detection"):
                prior = legacy_gnss_details(existing, old, self.cfg["gnss_jam"],
                                            self._gnss_cells(eid))
            details = merge_gnss(prior if old is not None else None, sig.details,
                                 self.cfg["gnss_jam"])
            summary, conf = gnss_summary(details), details["assessment"]["confidence"]
        # evidence distances from the event's own point (not from this pass's centroid)
        evidence = [{**e, "event_id": eid, "distance_km": round(haversine_km(
            lat, lon, e["details"]["lat"], e["details"]["lon"]), 1)}
            if "lat" in (e.get("details") or {}) else {**e, "event_id": eid}
            for e in sig.evidence]
        t_start, t_end, version, first_seen = sig.t_start, sig.t_end, 1, now_dt
        if old is not None:
            t_start = min(t_start, datetime.fromisoformat(old["t_start"]))
            t_end = max(t_end, datetime.fromisoformat(old["t_end"]))
            if sig.detector != "gnss_jam":
                conf = max(conf, float(old["confidence"]))
            known = {(r["kind"], r["ref_id"]) for r in self.store.list_corroborations(eid)}
            fresh = [e for e in sig.evidence if (e["kind"], e["ref_id"]) not in known]
            if (iso(t_start) == old["t_start"] and iso(t_end) == old["t_end"]
                    and round(conf, 4) == old["confidence"] and not fresh
                    and summary == old.get("summary")
                    and old.get("status") == SENSOR_DETECTED):
                return None
            version = int(old["version"]) + 1
            first_seen = datetime.fromisoformat(old["first_seen"])
            n_evidence = len(known | {(e["kind"], e["ref_id"]) for e in sig.evidence})
        else:
            n_evidence = len(sig.evidence)
        row = {
            "id": eid, "category": sig.category, "subcategory": sig.detector,
            "cameo_root": None, "title": sig.title if old is None else old["title"],
            "summary": summary, "actors": [], "lat": lat, "lon": lon,
            "precision": sig.precision,
            "place_name": (sig.place_name or _place(lat, lon)) if old is None
            else old["place_name"],
            "country": sig.country, "geo_confidence": 1.0 if sig.precision == "point" else 0.5,
            "h3_3": h3.latlng_to_cell(lat, lon, 3), "h3_5": h3.latlng_to_cell(lat, lon, 5),
            "h3_7": h3.latlng_to_cell(lat, lon, 7),
            "t_start": iso(t_start), "t_end": iso(t_end), "status": SENSOR_DETECTED,
            "confidence": round(conf, 4), "version": version, "mention_count": n_evidence,
            "source_diversity": 0, "gdelt_ids": [], "first_seen": iso(first_seen),
            "updated_at": iso(now_dt),
            "cluster_state": {"sensor": True, "detector": sig.detector,
                              "sensor_corroborated": False, "sensor_detected": True,
                              "v": CLUSTER_VERSION},
        }
        # the readings of the detecting sensor are evidence, not corroboration
        self.store.save_sensor_event(
            row, {"detector": sig.detector,
                  "dedupe_key": existing["dedupe_key"] if existing else sig.key,
                  "cell": existing["cell"] if existing else cell,
                  "details": details},
            evidence,
            {"event_id": eid, "score": 0.0,
             "kinds": sorted({e["kind"] for e in sig.evidence}), "evidence": n_evidence,
             "corroborated": False})
        return "created" if old is None else "updated"

    def _gnss_cells(self, eid: str) -> list[str]:
        return [r["ref_id"] for r in self.store.list_corroborations(eid)
                if r["kind"] == "gnss_cell"]

    def gnss_lifecycle(self, now: float) -> int:
        """Latest reading, series and lifecycle of recent GNSS events from the live cells
        (also once they are no longer hot). Returns the number of events updated."""
        c = self.cfg["gnss_jam"]
        snap = self.reader.get("gnss") or {}
        if not snap or now - float(snap.get("updated") or 0) > 1800:
            return 0
        t_snap = _dt(float(snap["updated"]) // 600 * 600)
        cells = {x["h3"]: x for x in snap.get("cells") or []}
        n_up = 0
        for row in self.store.recent_sensor_events(
                "gnss_jam", _dt(now) - timedelta(hours=float(c["track_h"]))):
            d = row.get("details") or {}
            if not d.get("detection"):
                ev = self.store.get_event(row["event_id"])
                if ev is None:
                    continue
                d = legacy_gnss_details(row, ev, c, self._gnss_cells(row["event_id"]))
            latest = d.get("latest") or {}
            if latest.get("t") == iso(t_snap):
                continue  # this 10-min slot is recorded (by detection or an earlier pass)
            if d.get("lifecycle") == "ended" and latest.get("t") and (
                    t_snap - datetime.fromisoformat(latest["t"])) < timedelta(hours=1):
                continue  # ended: an hourly reading is enough
            d = gnss_reading(d, [cells[h] for h in d.get("cells") or [] if h in cells],
                             t_snap, c)
            if self.store.update_sensor_event(row["event_id"], d, now=_dt(now),
                                              summary=gnss_summary(d),
                                              confidence=d["assessment"]["confidence"]):
                n_up += 1
        return n_up

    # ------------------------------------------------------------------ migration

    def migrate(self, lock_timeout: float = 0.0) -> dict[str, int] | None:
        """One-off, idempotent upgrade of stored sensor events (see module doc): status
        sensor-corroborated -> sensor-detected (not hazards), one-report squawk events
        removed, GNSS events re-assessed from their stored statistics. Runs under the event
        rebuild lock; None when it stays held (``run`` retries on later passes)."""
        from osint.pipeline.events import rebuild_lock_path
        from osint.store.lake import FileLock

        path = getattr(self.store, "path", None)
        lock = FileLock(rebuild_lock_path(Path(path)), timeout=lock_timeout) if path else None
        try:
            if lock is not None:
                lock.acquire()
        except TimeoutError:
            log.info("signals: sensor-event migration deferred (rebuild lock held)")
            return None
        try:
            return migrate_events(self.store, self.cfg, _dt(self.clock()))
        finally:
            if lock is not None:
                lock.release()

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
        # one-report squawks: never a high-severity alert (an earlier build may have raised
        # one); a low-severity note instead
        blips = {f"squawk:{ep['id']}" for ep in getattr(self, "transient", [])}
        self.alerts = [a for a in self.alerts if a["id"] not in blips]
        for ep in getattr(self, "transient", []):
            who = ep.get("flight") or ep.get("reg") or str(ep["hex"]).upper()
            self._alert(f"squawk-transient:{ep['id']}", now, "info", "squawk",
                        f"Transient squawk {ep['code']} ({reports(ep)} report): {who}",
                        "likely mis-set or dialled through; not an event", ep.get("lat"),
                        ep.get("lon"), at=float(ep["first_seen"]), transient=True)
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
        if self.cfg["gnss_jam"].get("enabled", True):
            try:
                out["gnss_lifecycle"] = self.gnss_lifecycle(now)
            except Exception:
                log.exception("signals: GNSS lifecycle update failed")
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

    async def _try_migrate(self) -> None:
        """Run the migration once per process; the event builder takes the rebuild lock on
        every pass, so wait briefly for it and retry on later passes until it succeeds."""
        if self._migrated:
            return
        try:
            res = await asyncio.to_thread(self.migrate, MIGRATE_LOCK_WAIT_S)
        except Exception:
            log.exception("signals: sensor-event migration failed")
            return
        if res is not None:
            self._migrated = True
            if any(res.values()):
                log.info("signals: sensor-event migration %s", res)

    async def run(self) -> None:
        self._stop.clear()
        await self._try_migrate()
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=float(self.cfg["initial_delay_s"]))
        except TimeoutError:
            pass
        while not self._stop.is_set():
            await self._try_migrate()
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

