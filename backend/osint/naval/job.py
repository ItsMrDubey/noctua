"""CarrierJob: gathers indicators from time-windowed lake/store queries, fuses them and keeps
the latest results cached for the API. CPU work runs in a worker thread."""

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from osint.core.geo import utcnow
from osint.naval import fusion, trackfit, voids
from osint.naval import indicators as I
from osint.naval.config import NavalConfig
from osint.naval.geo import Geo, get_geo
from osint.naval.models import Evidence
from osint.naval.store import NavalStore
from osint.naval.usni import fetch_latest

log = logging.getLogger(__name__)

DISCLAIMER = "Probabilistic estimate from public signals - not a confirmed position."
_NEWS_PREFILTER = ("carrier|cvn|strike group|amphibious ready|uss |hms |ins |liaoning|shandong|"
                   "fujian|cavour|trieste|juan carlos|kuznetsov|kaga|izumo|de gaulle|vikrant|"
                   "vikramaditya|航母|авианос")


class CarrierJob:
    def __init__(self, lake, store, settings, cfg: NavalConfig, nstore: NavalStore, *,
                 http=None, geo_factory: Callable[[], Geo] = get_geo) -> None:
        self.lake = lake
        self.store = store
        self.settings = settings
        self.cfg = cfg
        self.nstore = nstore
        self.http = http
        self._geo_factory = geo_factory
        self._lock = threading.Lock()
        self.cache: dict[int, tuple[float, dict]] = {}
        self.passes = 0
        self.last_error: str | None = None
        self.usni_error: str | None = None
        self._fits: list[trackfit.TrackFit] = []

    @property
    def interval_s(self) -> float:
        return float(self.cfg.sec("job").get("interval_s", 600))

    @property
    def default_hours(self) -> int:
        return int(self.cfg.sec("job").get("hours", 48))

    # ----------------------------------------------------------------- fetchers

    def _q(self, sql: str, params: list, start: datetime, end: datetime) -> list[tuple]:
        return self.lake.query(sql, params, start=start, end=end)

    def _air_rows(self, start: datetime, end: datetime) -> list[tuple]:
        return self._q(
            "SELECT track_id, epoch_us(ts), lat, lon, alt_ft, speed_kts, heading_deg "
            "FROM positions WHERE kind='air' AND ts >= ? AND ts <= ?", [start, end], start, end)

    def _ship_rows(self, ids: list[str], start: datetime, end: datetime) -> list[tuple]:
        out: list[tuple] = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            out += self._q(
                "SELECT track_id, epoch_us(ts), lat, lon, alt_ft, speed_kts, heading_deg "
                f"FROM positions WHERE kind='sea' AND ts >= ? AND ts <= ? AND track_id IN ({marks})",
                [start, end, *chunk], start, end)
        return out

    def _sea_count(self, start: datetime, end: datetime) -> int:
        return int(self._q("SELECT count(*) FROM positions WHERE kind='sea' AND ts >= ? AND ts <= ?",
                           [start, end], start, end)[0][0])

    def _warning_rows(self, now: datetime) -> list[dict]:
        days = float(self.cfg.sec("nav_warnings").get("lookback_days", 45))
        start = now - timedelta(days=days)
        rows = self._q(
            "SELECT id, url, title, attrs, epoch_us(published_at) FROM documents "
            "WHERE kind='nav_warning' AND published_at >= ? ORDER BY published_at DESC",
            [start], start, now + timedelta(hours=1))
        out = []
        for r in rows:
            try:
                attrs = json.loads(r[3] or "{}")
            except ValueError:
                attrs = {}
            out.append({"id": r[0], "url": r[1], "title": r[2], "attrs": attrs,
                        "published_us": r[4]})
        return out

    def _news_docs(self, now: datetime) -> list[dict]:
        hours = float(self.cfg.sec("news").get("lookback_hours", 72))
        start = now - timedelta(hours=hours)
        rows = self._q(
            "SELECT id, url, title, snippet, epoch_us(published_at), lat, lon FROM documents "
            "WHERE published_at >= ? AND kind <> 'nav_warning' AND lat IS NOT NULL AND "
            "regexp_matches(lower(coalesce(title,'') || ' ' || coalesce(snippet,'')), ?)",
            [start, _NEWS_PREFILTER], start, now + timedelta(hours=1))
        docs = [{"id": r[0], "url": r[1], "title": r[2], "snippet": r[3],
                 "ts": I._ts(r[4]), "lat": r[5], "lon": r[6], "kind": "doc"} for r in rows]
        try:
            events = self.store.list_events(since=start, include_unlocated=False, limit=5000)
        except Exception:  # the events store is owned elsewhere; never fail the pass on it
            log.warning("naval: event listing failed", exc_info=True)
            events = []
        pre = re.compile(_NEWS_PREFILTER, re.IGNORECASE)
        for e in events:
            if not pre.search(e.get("title") or ""):
                continue
            try:
                ts = datetime.fromisoformat(e.get("t_end") or e.get("updated_at"))
            except (TypeError, ValueError):
                ts = now
            docs.append({"id": e["id"], "url": f"/api/events/{e['id']}", "title": e.get("title"),
                         "snippet": "", "ts": ts, "lat": e.get("lat"), "lon": e.get("lon"),
                         "kind": "event"})
        return docs

    # -------------------------------------------------------------------- voids

    def _sea_bins(self, start: datetime, end: datetime, bin_s: int) -> list[tuple]:
        return self._q(
            f"SELECT h3_5, CAST(floor(epoch(ts) / {bin_s}) AS BIGINT) AS b, "
            "count(DISTINCT track_id) FROM positions WHERE kind='sea' AND ts >= ? AND ts < ? "
            "GROUP BY 1, 2", [start, end], start, end)

    def _void_evidence(self, now: datetime, geo: Geo, status: dict) -> list[Evidence]:
        vc = self.cfg.sec("voids")
        bin_h = float(vc.get("bin_hours", 6))
        bin_s = int(bin_h * 3600)
        now_bin = voids.bin_of(now.timestamp(), bin_h)
        recent_start = datetime.fromtimestamp((now_bin - 4) * bin_s, UTC)
        base = self.nstore.get_meta("void_baseline")
        fresh = base and time.time() - base.get("computed_at", 0) < float(
            vc.get("baseline_refresh_h", 6)) * 3600
        if not fresh:
            b_start = now - timedelta(days=float(vc.get("baseline_days", 14)))
            per_bin = voids.cells_res4(self._sea_bins(b_start, recent_start, bin_s))
            all_bins = sorted(per_bin)
            counts: dict[str, dict[int, int]] = {}
            for b, cells in per_bin.items():
                for c, n in cells.items():
                    counts.setdefault(c, {})[b] = n
            means = voids.baseline_means(counts, all_bins)
            total = (sum(sum(c.values()) for c in per_bin.values()) / len(all_bins)
                     if all_bins else 0.0)
            base = {"computed_at": time.time(), "bins": len(all_bins), "total_per_bin": total,
                    "cells": {c: list(v) for c, v in means.items() if v[0] >= 1.0}}
            self.nstore.set_meta("void_baseline", base)
        recent = voids.cells_res4(self._sea_bins(recent_start, now, bin_s))
        recent = {b: v for b, v in recent.items() if b < now_bin}
        rt = [sum(v.values()) for v in recent.values()]
        global_ratio = (sum(rt) / len(rt) / base["total_per_bin"]
                        if rt and base.get("total_per_bin") else 0.0)
        status["traffic_void"].update(baseline_bins=base.get("bins", 0),
                                      baseline_cells=len(base.get("cells", {})),
                                      global_ratio=round(global_ratio, 3))
        if base.get("bins", 0) < int(vc.get("min_baseline_bins", 12)):
            status["traffic_void"]["note"] = "baseline too short (needs >= 3 days of AIS)"
            return []
        found = voids.detect_voids({c: tuple(v) for c, v in base["cells"].items()}, recent,
                                   now_bin, vc, global_ratio=global_ratio,
                                   is_ocean=lambda c: not geo.mask.is_land_cell(c))
        evs = voids.void_evidence(found, now, float(vc.get("weight", 1.0)))
        if evs:
            tracks = self._transits(evs, now)
            evs += voids.diversion_evidence(evs, tracks, vc, now)
        return evs

    def _transits(self, vs: list[Evidence], now: datetime) -> dict[str, list[tuple]]:
        start = now - timedelta(hours=24)
        out: dict[str, list[tuple]] = {}
        for v in vs[:10]:
            d = (v.radius_km + 200) / 111.0
            rows = self._q(
                "SELECT track_id, min(epoch_us(ts)) AS t, avg(lat), avg(lon) FROM positions "
                "WHERE kind='sea' AND ts >= ? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
                "GROUP BY track_id, CAST(floor(epoch(ts) / 1800) AS BIGINT) ORDER BY 1, 2",
                [start, v.lat - d, v.lat + d, v.lon - 2 * d, v.lon + 2 * d], start, now)
            for tid, t, la, lo in rows:
                out.setdefault(tid, []).append((t, la, lo))
        return out

    # ------------------------------------------------------------------ compute

    def gather(self, hours: int, now: datetime, geo: Geo) -> tuple[list[Evidence], dict]:
        start = now - timedelta(hours=hours)
        status: dict[str, dict] = {k: {"inputs": 0, "evidence": 0, "note": ""} for k in (
            "sea_terminating_flight", "cvw_aircraft", "orbit", "support_orbit", "air_track",
            "support_ship",
            "nav_warning", "traffic_void", "traffic_diversion", "news", "usni_prior")}
        evidence: list[Evidence] = []
        self._fits = []  # set by the air indicator (moving-point fits of air fixes)

        def run(label: str, fn: Callable[[], list[Evidence]]) -> None:
            try:
                evs = fn()
            except Exception as exc:
                log.exception("naval indicator %s failed", label)
                status[label]["note"] = f"failed: {type(exc).__name__}: {exc}"[:200]
                return
            evidence.extend(evs)
            for e in evs:
                status[e.kind]["evidence"] += 1

        def air() -> list[Evidence]:
            ac = self.cfg.sec("aircraft")
            fit_start = min(start, now - timedelta(hours=float(ac.get("lookback_h", 168))))
            rows_all = self._air_rows(fit_start, now)
            start_us = int(start.timestamp() * 1_000_000)
            rows = [r for r in rows_all if r[1] >= start_us]
            ids = sorted({r[0] for r in rows_all})
            for k in ("sea_terminating_flight", "cvw_aircraft", "orbit", "support_orbit"):
                status[k]["inputs"] = len({r[0] for r in rows})
                if not rows:
                    status[k]["note"] = "no ADS-B positions in window"
            metas = self.store.get_tracks(ids) if ids else {}
            out = I.air_indicators(rows, metas, geo, self.cfg, window_start=start, now=now)
            fixes = I.air_fixes(rows_all, metas, geo, self.cfg, window_start=fit_start, now=now)
            tc = self.cfg.sec("trackfit")
            self._fits = trackfit.fit_tracks(fixes, tc)
            status["air_track"]["inputs"] = len(fixes)
            status["air_track"]["fits"] = len(self._fits)
            if not fixes:
                status["air_track"]["note"] = (
                    f"no launch/recovery/loiter fixes in the last {ac.get('lookback_h', 168)} h")
            return out + trackfit.fit_evidence(self._fits, now, tc, geo.mask.is_ocean)

        def ships() -> list[Evidence]:
            cands = I.candidate_ship_ids(self.settings.sqlite_path, self.cfg)
            status["support_ship"]["inputs"] = len(cands)
            loiter = float(self.cfg.sec("ships").get("loiter_hours", 12))
            s = min(start, now - timedelta(hours=loiter))
            if not cands:
                status["support_ship"]["note"] = "no fleet-support / AIS type-35 tracks known"
                return []
            return I.ship_indicators(self._ship_rows(sorted(cands), s, now), cands, geo,
                                     self.cfg, now=now)

        def warnings() -> list[Evidence]:
            rows = self._warning_rows(now)
            status["nav_warning"]["inputs"] = len(rows)
            cur = self.store.get_cursor("nga_msi") or {}
            active = set(cur["active"]) if isinstance(cur.get("active"), list) else None
            if not rows:
                status["nav_warning"]["note"] = "no NGA warnings stored (collector nga_msi)"
            return I.warning_indicators(rows, active, geo, self.cfg, now=now)

        def traffic() -> list[Evidence]:
            n = self._sea_count(now - timedelta(hours=6), now)
            status["traffic_void"]["inputs"] = n
            if n == 0:
                status["traffic_void"]["note"] = status["traffic_diversion"]["note"] = (
                    "no AIS positions in the last 6 h (AIS not configured?)")
                return []
            return self._void_evidence(now, geo, status)

        def news() -> list[Evidence]:
            docs = self._news_docs(now)
            status["news"]["inputs"] = len(docs)
            return I.news_indicators(docs, self.cfg, now=now)

        def prior() -> list[Evidence]:
            st = self.nstore.get_meta("usni")
            status["usni_prior"]["inputs"] = len((st or {}).get("entries", []))
            if not st:
                status["usni_prior"]["note"] = self.usni_error or "USNI tracker not fetched yet"
            return I.usni_prior(st, self.cfg, now=now)

        for label, fn in (("sea_terminating_flight", air), ("support_ship", ships),
                          ("nav_warning", warnings), ("traffic_void", traffic),
                          ("news", news), ("usni_prior", prior)):
            run(label, fn)
        return evidence, status

    def compute(self, hours: int | None = None, *, now: datetime | None = None,
                persist: bool = True) -> dict:
        hours = int(hours or self.default_hours)
        with self._lock:
            now = now or utcnow()
            geo = self._geo_factory()
            evidence, status = self.gather(hours, now, geo)
            fc = self.cfg.sec("fusion")
            usni = self.nstore.get_meta("usni")
            surface = fusion.build_surface(evidence, now, fc, geo.mask)
            hyps = fusion.hypotheses(evidence, surface, fc, usni=usni, usni_cfg=self.cfg.sec("usni"))
            self._assign_ids(hyps, now)
            if persist:
                self.nstore.save(hyps, now)
                self.nstore.prune_history(now - timedelta(days=30))
            no_prior = [e for e in evidence if e.kind != "usni_prior"]
            s2 = fusion.build_surface(no_prior, now, fc, geo.mask)
            eval_hyps = fusion.hypotheses(no_prior, s2, fc)
            for h in eval_hyps:
                near = min(hyps, key=lambda x: abs(x["lat"] - h["lat"]) + abs(x["lon"] - h["lon"]),
                           default=None)
                h["id"] = near["id"] if near else None
            tc = self.cfg.sec("trackfit")
            fits = [f.to_dict(now, tc, geo.mask.is_ocean) for f in self._fits]
            result = {
                "generated_at": now.isoformat(), "hours": hours, "disclaimer": DISCLAIMER,
                "air_tracks": fits,
                "hypotheses": hyps, "surface": surface.cells_out(),
                "evidence": [e.to_dict() for e in evidence], "indicator_status": status,
                "eval": fusion.evaluate(usni, eval_hyps),
            }
            self.cache[hours] = (time.monotonic(), result)
            return result

    def _assign_ids(self, hyps: list[dict], now: datetime) -> None:
        fc = self.cfg.sec("fusion")
        prev = self.nstore.recent(now - timedelta(hours=float(fc.get("match_hours", 24))))
        taken: set[str] = set()
        for h in hyps:
            m = self.nstore.match(h["lat"], h["lon"], prev, float(fc.get("match_km", 300)), taken)
            if m is not None:
                taken.add(m["id"])
                h["id"], h["first_seen"] = m["id"], m["first_seen"]
            else:
                h["id"], h["first_seen"] = "csg-" + uuid.uuid4().hex[:10], now.isoformat(
                    timespec="seconds")
            h["last_updated"] = now.isoformat(timespec="seconds")
            h["label"] = f"CSG? {h['confidence']:.2f}"
            h["disclaimer"] = DISCLAIMER

    # ---------------------------------------------------------------- serving

    async def get(self, hours: int | None = None) -> dict:
        hours = int(hours or self.default_hours)
        hit = self.cache.get(hours)
        if hit is not None and time.monotonic() - hit[0] < self.interval_s:
            return hit[1]
        return await asyncio.to_thread(self.compute, hours, persist=hours == self.default_hours)

    async def refresh_usni(self) -> None:
        if self.http is None:
            return
        try:
            state = await fetch_latest(self.http, self.cfg.sec("usni"),
                                       await asyncio.to_thread(self.nstore.get_meta, "usni"))
            if state is not None:
                await asyncio.to_thread(self.nstore.set_meta, "usni", state)
            self.usni_error = None
        except Exception as exc:  # noqa: BLE001 - calibration feed; never fails the pass
            self.usni_error = f"USNI fetch failed: {type(exc).__name__}: {exc}"[:200]
            log.warning("naval: %s", self.usni_error)

    async def run_once(self) -> dict:
        await self.refresh_usni()
        return await asyncio.to_thread(self.compute, self.default_hours)

    async def run(self) -> None:
        try:
            await asyncio.sleep(float(self.cfg.sec("job").get("initial_delay_s", 90)))
            while True:
                try:
                    await self.run_once()
                    self.last_error = None
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("naval carrier pass failed")
                self.passes += 1
                await asyncio.sleep(self.interval_s)
        finally:
            self.nstore.close()


def get_job(runtime) -> CarrierJob:
    """The runtime's CarrierJob, created on first use (shared by the background task and API)."""
    job = getattr(runtime, "naval_job", None)
    if job is None:
        cfg = NavalConfig.load(runtime.settings.config_dir / "naval.yaml")
        job = CarrierJob(runtime.lake, runtime.store, runtime.settings, cfg,
                         NavalStore(runtime.settings.data_dir / "naval.sqlite"),
                         http=getattr(runtime, "http", None))
        runtime.naval_job = job
    return job


def start(runtime) -> asyncio.Task:
    """Create the background task (one-line wiring from Runtime._boot)."""
    task = asyncio.create_task(get_job(runtime).run(), name="naval")
    runtime._tasks.append(task)
    return task
