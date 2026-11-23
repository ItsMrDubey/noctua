"""ADS-B signals: GNSS interference map and emergency squawks, from all aircraft (not only
military) in watch regions of a readsb-compatible open API (default adsb.fi opendata).

Per poll: one ``point`` query per watch region (``regions``: lat, lon, radius <= 250 NM),
then one ``sqk`` query per emergency code (worldwide), ``spacing_s`` apart (adsb.fi allows
1 request/s; a 429 ends the cycle early). Every aircraft's navigation integrity feeds the
GNSS aggregator (``osint.signals.gnss``); emergency codes feed the squawk tracker
(``osint.signals.aviation``).

Naval capture (``naval_capture``, default on): region aircraft of carrier-associated /
maritime-patrol types or listed callsigns that are NOT flagged military (so the ``adsb``
collector misses them) are also written as lake positions (source ``adsb_signals:naval``)
for the carrier model - no extra requests (``osint.naval.capture``).

Writes ``signals/gnss.json`` (live cells) and ``signals/squawks.json`` every poll. Lake
observations: ``gnss`` (one per H3 cell and closed hour: value = % degraded, attrs n / bad)
and ``squawk`` (one per episode, re-versioned every 10 min while it lasts).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

import h3
import httpx

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.models import Batch, Observation
from osint.signals.aviation import SEVERITY, SquawkTracker
from osint.signals.cache import write_signal
from osint.signals.gnss import GnssAggregator, nav_sample

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://opendata.adsb.fi/api/v2"
DEFAULT_POINT_PATH = "/lat/{lat}/lon/{lon}/dist/{dist}"
DEFAULT_SQK_PATH = "/sqk/{code}"
# Hotspots with documented GNSS interference plus two quiet control areas (Central Europe,
# US Midwest) so "normal" is visible on the map. Radius in NM (API maximum 250).
DEFAULT_REGIONS: list[dict[str, Any]] = [
    {"name": "Baltic / Kaliningrad", "lat": 55.5, "lon": 20.0, "dist": 250},
    {"name": "Gulf of Finland", "lat": 60.0, "lon": 27.0, "dist": 200},
    {"name": "Poland / Belarus", "lat": 52.5, "lon": 24.5, "dist": 200},
    {"name": "Black Sea", "lat": 44.0, "lon": 33.5, "dist": 250},
    {"name": "Eastern Mediterranean", "lat": 34.0, "lon": 33.5, "dist": 250},
    {"name": "Levant", "lat": 32.0, "lon": 36.0, "dist": 200},
    {"name": "Iraq", "lat": 33.0, "lon": 44.0, "dist": 250},
    {"name": "Persian Gulf", "lat": 26.5, "lon": 52.0, "dist": 250},
    {"name": "Caucasus", "lat": 41.5, "lon": 45.0, "dist": 200},
    {"name": "Red Sea", "lat": 20.0, "lon": 39.0, "dist": 250},
    {"name": "Libya", "lat": 32.0, "lon": 15.0, "dist": 250},
    {"name": "Korea", "lat": 37.5, "lon": 127.0, "dist": 200},
    {"name": "India / Pakistan", "lat": 30.5, "lon": 73.5, "dist": 250},
    {"name": "Central Europe (control)", "lat": 50.0, "lon": 9.0, "dist": 200},
    {"name": "US Midwest (control)", "lat": 40.0, "lon": -90.0, "dist": 200},
]
EMERGENCY_CODES = ("7500", "7600", "7700")
SQUAWK_VERSION_S = 600  # a running episode's lake row is refreshed this often


class RateLimited(Exception):
    pass


def _aircraft(data: dict) -> tuple[float, list[dict]]:
    """(now in unix seconds, aircraft) from readsb (``ac``, ms) / adsb.fi (``aircraft``, s)."""
    now = data.get("now")
    now_s = float(now) if isinstance(now, (int, float)) else time.time()
    if now_s > 1e11:
        now_s /= 1000.0
    ac = data.get("ac") if data.get("ac") is not None else data.get("aircraft")
    return now_s, [a for a in (ac or []) if isinstance(a, dict)]


def squawk_observation(ep: dict) -> Observation | None:
    lat, lon = ep.get("lat"), ep.get("lon")
    if lat is None or lon is None:
        return None
    last = float(ep["last_seen"])
    return Observation(
        id=f"squawk:{ep['id']}", type="squawk", version=str(int(last // SQUAWK_VERSION_S)),
        ts=datetime.fromtimestamp(last, UTC), lat=lat, lon=lon, source="adsb_signals",
        value=float(ep["code"]) if ep["code"].isdigit() else None,
        attrs={k: ep.get(k) for k in (
            "hex", "code", "label", "flight", "reg", "type", "desc", "squawk", "emergency",
            "alt_ft", "gs", "first_seen", "last_seen", "first_lat", "first_lon", "mil",
            "source")} | {"severity": SEVERITY.get(ep["code"], "warn")})


def gnss_observation(hour: int, cell: str, n: int, bad: int, res: int) -> Observation:
    lat, lon = h3.cell_to_latlng(cell)
    return Observation(
        id=f"gnss:{cell}:{hour}", type="gnss", ts=datetime.fromtimestamp(hour, UTC),
        lat=lat, lon=lon, source="adsb_signals", value=round(100.0 * bad / n, 2) if n else 0.0,
        attrs={"cell": cell, "res": res, "n": n, "bad": bad})


@register
class AdsbSignalsCollector(PollCollector):
    name = "adsb_signals"
    domain = "aviation"
    licence = "adsb.fi open data: non-commercial use"
    commercial_ok = False
    default_interval_s = 120.0
    poll_timeout_s = 100.0

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.base = str(cfg.get("base") or DEFAULT_BASE).rstrip("/")
        self.point_path = str(cfg.get("point_path") or DEFAULT_POINT_PATH)
        self.sqk_path = str(cfg.get("sqk_path") or DEFAULT_SQK_PATH)
        self.spacing_s = float(cfg.get("spacing_s", 1.2))
        self.regions = list(cfg.get("regions") or DEFAULT_REGIONS)
        self.codes = tuple(str(c) for c in cfg.get("squawks") or EMERGENCY_CODES)
        self.gnss = GnssAggregator(cfg.get("gnss") or {})
        sq = cfg.get("squawk") or {}
        self.squawks = SquawkTracker(
            gap_s=float(sq.get("gap_s", 1800)), keep_s=float(sq.get("keep_h", 24)) * 3600,
            ignore_status=tuple(sq.get("ignore_status") or ("lifeguard",)))
        self._loaded = False
        self.sleep = asyncio.sleep  # injectable for tests
        self.region_status: dict[str, dict] = {}
        self.naval_capture = bool(cfg.get("naval_capture", True))
        self._capture = None
        self._mil_db = None

    async def _init_capture(self, ctx: CollectorContext) -> None:
        """Load the naval capture filter once (disabled when settings lack the dirs)."""
        cfg_dir = getattr(ctx.settings, "config_dir", None)
        ref_dir = getattr(ctx.settings, "reference_dir", None)
        if not self.naval_capture or cfg_dir is None or ref_dir is None:
            self.naval_capture = False
            return
        from osint.enrich.military import MilDb
        from osint.naval.capture import NavalCapture
        try:
            self._capture = await asyncio.to_thread(NavalCapture.from_config_dir, cfg_dir)
            self._mil_db = await asyncio.to_thread(MilDb.from_csv,
                                                   ref_dir / "plane-alert-mil.csv")
        except Exception:  # optional side feature; never break GNSS / squawks
            log.warning("adsb_signals: naval capture disabled", exc_info=True)
            self.naval_capture = False

    def _naval(self, batch: Batch, captured: list[tuple[float, dict]]) -> None:
        from osint.collectors.adsb import parse_aircraft
        seen: set[str] = set()
        for now_s, ac in captured:
            why = self._capture.reason(ac) if self._capture else None
            hx = str(ac.get("hex") or "").lower()
            if why is None or hx in seen:
                continue
            seen.add(hx)
            try:
                parsed = parse_aircraft(ac, now_s, self._mil_db, "adsb_signals:naval")
            except (ValueError, TypeError, KeyError):
                continue
            if parsed is None:
                continue
            pos, track = parsed
            track.extra = {**track.extra, "naval_capture": why}
            batch.tracks.append(track)
            if pos is not None:
                batch.positions.append(pos)

    async def _get(self, ctx: CollectorContext, url: str) -> dict:
        r = await ctx.http.get(url, timeout=20)
        if r.status_code == 429:
            raise RateLimited(url)
        r.raise_for_status()
        return r.json()

    async def poll(self, ctx: CollectorContext) -> Batch:
        if not self._loaded:
            self.squawks.load_cursor(ctx.get_cursor(self.name))
            await self._init_capture(ctx)
            self._loaded = True
        captured: list[tuple[float, dict]] = []
        requests: list[tuple[str, str]] = [
            ("region:" + str(r.get("name") or f"{r['lat']},{r['lon']}"),
             self.base + self.point_path.format(lat=r["lat"], lon=r["lon"],
                                                dist=int(r.get("dist", 250))))
            for r in self.regions]
        requests += [("sqk:" + c, self.base + self.sqk_path.format(code=c)) for c in self.codes]
        ok = failed = 0
        now_s = time.time()
        emergencies: list[dict] = []
        samples = []
        limited = False
        for i, (label, url) in enumerate(requests):
            if i:
                await self.sleep(self.spacing_s)
            try:
                now_s, aircraft = _aircraft(await self._get(ctx, url))
            except RateLimited:
                limited = True
                log.info("adsb_signals: rate limited at %s; skipping the rest of this cycle",
                         label)
                break
            except (httpx.HTTPError, ValueError) as exc:
                failed += 1
                log.debug("adsb_signals: %s failed: %s", label, exc)
                if label.startswith("region:"):
                    self.region_status[label[7:]] = {"ok": False, "error": str(exc)[:200]}
                continue
            ok += 1
            if self.naval_capture and label.startswith("region:"):
                captured += [(now_s, a) for a in aircraft]
            for ac in aircraft:
                s = nav_sample(ac, now_s, self.gnss.cfg)
                if s is not None:
                    samples.append(s)
            emergencies += aircraft if label.startswith("sqk:") else [
                a for a in aircraft if a.get("squawk") in self.codes
                or str(a.get("emergency") or "none") not in ("none", "")]
            if label.startswith("region:"):
                self.region_status[label[7:]] = {"ok": True, "aircraft": len(aircraft),
                                                 "at": round(now_s)}
        if not ok:
            raise RuntimeError("adsb_signals: every request failed"
                               + (" (rate limited)" if limited else ""))
        self.gnss.add(samples)
        started = self.squawks.update(emergencies, now_s, source=self.base)
        self.squawks.prune(now_s)
        batch = Batch(source=self.name)
        if self.naval_capture and captured:
            self._naval(batch, captured)
        res = self.gnss.res
        for hour, cell, n, bad in self.gnss.pop_closed_hours(now_s):
            batch.observations.append(gnss_observation(hour, cell, n, bad, res))
        new_ids = {e["id"] for e in started}
        for ep in self.squawks.items(now_s):
            if ep["active"] or ep["id"] in new_ids:
                obs = squawk_observation(ep)
                if obs is not None:
                    batch.observations.append(obs)
        ctx.set_cursor(self.name, self.squawks.to_cursor())
        self._write(ctx, now_s, len(samples), limited)
        return batch

    def _write(self, ctx: CollectorContext, now_s: float, n_samples: int, limited: bool) -> None:
        data_dir = getattr(ctx.settings, "data_dir", None)
        if data_dir is None:
            return
        cfg = self.gnss.cfg
        regions = [{"name": str(r.get("name") or ""), "lat": r["lat"], "lon": r["lon"],
                    "dist_nm": int(r.get("dist", 250)),
                    **self.region_status.get(str(r.get("name") or f"{r['lat']},{r['lon']}"), {})}
                   for r in self.regions]
        try:
            write_signal(data_dir, "gnss", {
                "updated": now_s, "res": self.gnss.res, "window_min": cfg["window_min"],
                "thresholds": {k: cfg[k] for k in ("nic_min", "nacp_min", "medium_pct",
                                                   "high_pct", "min_aircraft")},
                "samples": n_samples, "rate_limited": limited, "regions": regions,
                "cells": self.gnss.snapshot(now_s)})
            write_signal(data_dir, "squawks", {"updated": now_s,
                                               "items": self.squawks.items(now_s)})
        except OSError:
            log.exception("adsb_signals: writing the live snapshot failed")
