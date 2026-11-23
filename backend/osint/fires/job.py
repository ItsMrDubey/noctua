"""FireScoreJob (worker only): conflict likelihood of FIRMS hotspots.

Every ``interval_s`` it refreshes the reference data that is due (DeepStateMap front line
every ``frontline.refresh_h``; GVP volcanoes and EOG flares every ``reference.refresh_days``),
then scores, in a worker thread, the hotspots of the last ``window_h`` that have no score yet
(or one computed with an older config). Every ``rescore_h`` the whole window is rescored, so
new conflict news, a moved front line and later detections (cluster size) are picked up.

Context is held in memory and rebuilt only when due: recurrence per ~450 m cell from the
lake history (DuckDB aggregate, every ``history.refresh_h``), UCDP GED density (daily),
recent conflict news (every pass), cached reference files (when their mtime changes).
All external fetches happen here, never on an API request.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from osint.core.geo import utcnow
from osint.fires import frontline, reference
from osint.fires.config import FireScoreConfig
from osint.fires.geo import PointIndex, SegmentIndex
from osint.fires.score import Context, Hotspot, Pool, Recurrence, score_hotspots
from osint.fires.store import FireScoreStore, get_score_store, iso

log = logging.getLogger(__name__)

DAY_US = 86_400_000_000
RETRY_S = 1800.0  # a failed download is retried after this long
UCDP_REFRESH = timedelta(hours=24)
STATUS_KEY = "status"


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _attrs(raw: str | None) -> dict:
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


class FireScoreJob:
    def __init__(self, lake, store, scores: FireScoreStore, cfg: FireScoreConfig, *,
                 ref_dir: Path, gazetteer_dir: Path, http: httpx.AsyncClient | None = None,
                 pois_loader=reference.load_pois, clock=utcnow) -> None:
        self.lake, self.store, self.scores, self.cfg = lake, store, scores, cfg
        self.ref_dir, self.gazetteer_dir, self.http = ref_dir, gazetteer_dir, http
        self.pois_loader, self.clock = pois_loader, clock
        self.ctx = Context()
        self._mtimes: dict[str, float | None] = {}
        self._loaded_at: dict[str, datetime] = {}
        self._retry_at: dict[str, float] = {}
        self._fetched_at: dict[str, float] = {}  # this process's downloads (else file mtime)
        self._last_full: datetime | None = None
        self.passes = 0
        self.scored = 0
        self.last_run_at: str | None = None
        self.last_error: str | None = None
        self.fetch_errors: dict[str, str] = {}

    # ------------------------------------------------------------ reference data

    def _due_file(self, name: str, max_age_s: float, now: datetime) -> bool:
        if self._retry_at.get(name, 0.0) > now.timestamp():
            return False
        last = self._fetched_at.get(name) or _mtime(self.ref_dir / name)
        return last is None or now.timestamp() - last >= max_age_s

    async def _download(self, name: str, coro, now: datetime) -> None:
        try:
            n = await coro
            self._fetched_at[name] = now.timestamp()
            self.fetch_errors.pop(name, None)
            log.info("fire score: refreshed %s (%s)", name, n)
        except Exception as exc:  # noqa: BLE001 — kept cache; retried after RETRY_S
            self._retry_at[name] = now.timestamp() + RETRY_S
            self.fetch_errors[name] = str(exc) or type(exc).__name__
            log.warning("fire score: %s refresh failed: %s", name, exc)

    async def refresh_reference(self, now: datetime) -> None:
        if self.http is None:
            return
        cfg = self.cfg
        if cfg.frontline.enabled and self._due_file(frontline.CACHE_NAME,
                                                    cfg.frontline.refresh_h * 3600, now):
            async def front() -> str:
                payload = await frontline.fetch(self.http, cfg.frontline.url)
                rec = await asyncio.to_thread(frontline.build, payload, self.ref_dir,
                                              cfg.frontline.probe_km, now)
                await asyncio.to_thread(frontline.save, rec, self.ref_dir)
                return f"{rec['length_km']} km, map {rec['source_datetime']}"
            await self._download(frontline.CACHE_NAME, front(), now)
        max_age = cfg.reference.refresh_days * 86400
        if cfg.reference.volcano_url and self._due_file(reference.VOLCANO_FILE, max_age, now):
            await self._download(reference.VOLCANO_FILE, reference.fetch_volcanoes(
                self.http, cfg.reference.volcano_url, self.ref_dir), now)
        if cfg.reference.flare_url and self._due_file(reference.FLARE_FILE, max_age, now):
            await self._download(reference.FLARE_FILE, reference.fetch_flares(
                self.http, cfg.reference.flare_url, self.ref_dir), now)

    def _changed(self, name: str) -> bool:
        mt = _mtime(self.ref_dir / name)
        if name in self._mtimes and self._mtimes[name] == mt:
            return False
        self._mtimes[name] = mt
        return True

    def _due(self, key: str, every: timedelta, now: datetime) -> bool:
        at = self._loaded_at.get(key)
        if at is not None and now - at < every:
            return False
        self._loaded_at[key] = now
        return True

    # ------------------------------------------------------------ context

    def load_context(self, now: datetime) -> None:
        ctx, cfg = self.ctx, self.cfg
        if self._changed(frontline.CACHE_NAME):
            rec = frontline.load(self.ref_dir)
            ctx.frontline = SegmentIndex(frontline.lines_latlon(rec)) if rec else None
            ctx.meta["frontline"] = None if rec is None else {
                k: rec.get(k) for k in ("source_datetime", "fetched_at", "length_km",
                                        "clipped_to_ukraine")}
        if self._changed(reference.VOLCANO_FILE):
            vs = reference.load_volcanoes(self.ref_dir, cfg.reference.volcano_since_year)
            ctx.volcanoes = PointIndex((v["lat"], v["lon"], v["name"]) for v in vs) \
                if vs else None
            ctx.meta["volcanoes"] = len(vs)
        if self._changed(reference.FLARE_FILE):
            fl = reference.load_flares(self.ref_dir)
            ctx.flares = PointIndex(((f[0], f[1], f[2] if len(f) > 2 else "") for f in fl),
                                    cell_deg=0.05) if fl else None
            ctx.meta["flares"] = len(fl)
        if "cities" not in self._loaded_at:
            self._loaded_at["cities"] = now
            cities = reference.load_cities(self.gazetteer_dir, cfg.pattern.urban_min_pop)
            ctx.cities = PointIndex(((la, lo, (name, reference.urban_radius_km(pop)))
                                     for la, lo, name, pop in cities)) if cities else None
            ctx.meta["cities"] = len(cities)
            try:
                pois = self.pois_loader()
            except Exception:
                log.exception("fire score: reference POIs unavailable")
                pois = []
            ctx.pois = PointIndex(((la, lo, (name, grp)) for la, lo, name, grp in pois)) \
                if pois else None
            ctx.meta["pois"] = len(pois)
        if self._due("ucdp", UCDP_REFRESH, now):
            ev = self.store.list_events(
                since=now - timedelta(days=365.25 * cfg.context.ucdp_years),
                origin="historical", category="armed_conflict", include_unlocated=False,
                limit=5_000_000)
            pts = [(e["lat"], e["lon"], None) for e in ev
                   if e.get("precision") in ("point", "city")]
            ctx.ucdp = PointIndex(pts, cell_deg=0.25) if pts else None
            ctx.meta["ucdp"] = len(pts)
        if self._due("history", timedelta(hours=cfg.history.refresh_h), now):
            ctx.recurrence = self.recurrence(now)
            ctx.meta["history_days"] = ctx.recurrence.history_days
            ctx.meta["static_cells"] = len(ctx.recurrence.cells)
        c = cfg.context
        news = self.store.list_events(
            since=now - timedelta(hours=c.news_hours), category=",".join(c.news_categories),
            origin="news", include_unlocated=False, limit=20000)
        pts = [(e["lat"], e["lon"], (e["id"], e.get("title") or "", e.get("precision")))
               for e in news if e.get("precision") in c.news_precisions]
        ctx.news = PointIndex(pts) if pts else None
        ctx.meta["news"] = len(pts)

    def recurrence(self, now: datetime) -> Recurrence:
        """Past-day masks of cells seen on >= 2 days before today (UTC) in the lake."""
        h = self.cfg.history
        days = max(1, min(62, int(h.days)))
        ref_day = int(now.timestamp() * 1_000_000) // DAY_US
        start = datetime.fromtimestamp((ref_day - days) * 86400, now.tzinfo)
        end = datetime.fromtimestamp(ref_day * 86400, now.tzinfo)
        deg = float(h.cell_deg)
        span = [start, end - timedelta(microseconds=1)]
        avail = self.lake.query(
            "SELECT count(DISTINCT epoch_us(ts) // ?) FROM observations "
            "WHERE type = 'fire' AND ts >= ? AND ts <= ?", [DAY_US, *span],
            start=start, end=end)
        n_days = int(avail[0][0] or 0) if avail else 0
        cells: dict[tuple[int, int], tuple[int, float | None]] = {}
        if n_days:
            rows = self.lake.query(
                "SELECT CAST(floor(lat / ?) AS BIGINT), CAST(floor(lon / ?) AS BIGINT), "
                "bit_or(CAST(1 AS UBIGINT) << CAST(? - 1 - epoch_us(ts) // ? AS UBIGINT)), "
                "median(value) FROM observations WHERE type = 'fire' AND ts >= ? AND ts <= ? "
                "GROUP BY 1, 2 HAVING count(DISTINCT epoch_us(ts) // ?) >= 2",
                [deg, deg, ref_day, DAY_US, *span, DAY_US], start=start, end=end)
            cells = {(cy, cx): (int(mask), med) for cy, cx, mask, med in rows}
        return Recurrence(cells, deg, n_days)

    # ------------------------------------------------------------ scoring

    def window(self, now: datetime) -> list[Hotspot]:
        start = now - timedelta(hours=self.cfg.job.window_h)
        rows = self.lake.query(
            "SELECT id, epoch_us(ts), lat, lon, value, attrs FROM observations "
            "WHERE type = 'fire' AND ts >= ? AND ts <= ?", [start, now], start=start, end=now)
        out: dict[str, Hotspot] = {}
        for oid, us, lat, lon, frp, attrs in rows:
            if lat is None or lon is None or oid in out:
                continue
            a = _attrs(attrs)
            out[oid] = Hotspot(id=oid, ts_us=us, lat=lat, lon=lon, frp=frp,
                               daynight=str(a.get("daynight") or ""),
                               confidence=str(a.get("confidence") or ""))
        return list(out.values())

    def score_pass(self, now: datetime) -> int:
        cfg = self.cfg
        self.load_context(now)
        pool = self.window(now)
        start = now - timedelta(hours=cfg.job.window_h)
        fp = cfg.fingerprint()
        full = self._last_full is None or now - self._last_full >= timedelta(
            hours=cfg.job.rescore_h)
        if full:
            targets = pool
            self._last_full = now
        else:
            known = self.scores.fingerprints(start, now)
            targets = [h for h in pool if known.get(h.id) != fp]
        n = 0
        if targets:
            rows = score_hotspots(targets, Pool(pool), self.ctx, cfg)
            by_id = {h.id: h for h in targets}
            for r in rows:
                h = by_id[r["id"]]
                r.update(ts=datetime.fromtimestamp(h.ts_us / 1e6, now.tzinfo), lat=h.lat,
                         lon=h.lon)
            n = self.scores.save(rows, fp, now)
        self.scores.prune(now - timedelta(days=cfg.job.retention_days))
        self.scores.set_meta(STATUS_KEY, {
            "last_run_at": iso(now), "scored_last_pass": n, "full_rescore": full,
            "window_h": cfg.job.window_h, "window_hotspots": len(pool),
            **self.scores.stats(start, now, cfg.job.threshold),
            "context": dict(self.ctx.meta), "fetch_errors": dict(self.fetch_errors)})
        return n

    # ------------------------------------------------------------ loop

    async def run_once(self) -> int:
        now = self.clock()
        await self.refresh_reference(now)
        n = await asyncio.to_thread(self.score_pass, now)
        self.scored += n
        self.last_run_at = iso(now)
        return n

    async def run(self) -> None:
        if not self.cfg.job.enabled:
            log.info("fire scoring disabled (config/fire_score.yaml)")
            return
        await asyncio.sleep(self.cfg.job.initial_delay_s)
        while True:
            try:
                n = await self.run_once()
                self.last_error = None
                if n:
                    log.info("fire score pass: %d hotspots scored", n)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("fire score pass failed")
            self.passes += 1
            await asyncio.sleep(self.cfg.job.interval_s)

    def status(self) -> dict:
        return {"enabled": self.cfg.job.enabled, "passes": self.passes, "scored": self.scored,
                "last_run_at": self.last_run_at, "last_error": self.last_error,
                "fetch_errors": dict(self.fetch_errors)}


# ------------------------------------------------------------------- wiring


def build_job(runtime) -> FireScoreJob:
    settings = runtime.settings
    cfg = FireScoreConfig.load(settings.config_dir / "fire_score.yaml")
    return FireScoreJob(runtime.lake, runtime.store, get_score_store(runtime), cfg,
                        ref_dir=settings.data_dir / "reference",
                        gazetteer_dir=settings.data_dir / "gazetteer",
                        http=getattr(runtime, "http", None))


def start(runtime) -> asyncio.Task:
    """Create the job (as runtime.fire_score_job) and start its loop as a task."""
    job = build_job(runtime)
    runtime.fire_score_job = job
    task = asyncio.create_task(job.run(), name="fire_score")
    runtime._tasks.append(task)
    return task
