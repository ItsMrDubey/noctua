"""ImpactJob (worker only): impact sites + affected areas for recent events.

Every ``interval_s`` it looks at recent located events and, for those whose inputs changed
(extracted impact sites, the event's anchor location, fire/quake corroboration) or whose
record is older than ``refresh_h`` while the event is fresh, runs the geocoding cascade
(osint.enrich.impact) and stores one ``event_impacts`` row per event. The EventBuilder
then upgrades the event's location to the best site (pipeline.events.apply_impacts).

All external lookups (Nominatim, USGS) happen here, never on an API request.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import yaml

from osint.core.geo import haversine_km, utcnow
from osint.enrich.impact import (
    FINE,
    IMPACT_VERSION,
    HttpCache,
    ImpactResolver,
    Nominatim,
    ReferencePOIs,
    Usgs,
    best_site,
    buffered_hull,
    circle,
    felt_radius_km,
    geometry_bounds,
    mmi_area,
    site_area,
    union_bounds,
    utc_iso,
)

log = logging.getLogger(__name__)

FIRE_WORDS = re.compile(r"\b(fire|fires|wildfire|wildfires|blaze|bushfire|forest fire)\b",
                        re.IGNORECASE)
QUAKE_WORDS = re.compile(r"\b(earthquake|quake|tremor|seismic)\b", re.IGNORECASE)
ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII", 8: "VIII",
         9: "IX", 10: "X"}


@dataclass
class ImpactConfig:
    enabled: bool = True
    interval_s: float = 60.0
    initial_delay_s: float = 90.0
    event_age_h: float = 168.0
    select_limit: int = 3000
    max_events_per_pass: int = 10
    refresh_h: float = 6.0         # recompute fresh events this often (FIRMS / ShakeMap)
    refresh_event_age_h: float = 48.0
    nominatim_enabled: bool = True
    nominatim_daily_cap: int = 500
    usgs_enabled: bool = True
    firms_snap_km: float = 3.0
    firms_snap_h: float = 12.0
    fire_area_km: float = 10.0
    fire_buffer_km: float = 0.4
    felt_min_mag: float = 3.0
    categories: list[str] = field(default_factory=lambda: [
        "armed_conflict", "disaster", "infrastructure", "maritime_security", "air_activity",
        "civil_unrest"])

    @classmethod
    def load(cls, path: Path) -> ImpactConfig:
        data = {}
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _close(a: dict | None, lat, lon) -> bool:
    return bool(a) and a.get("lat") is not None and lat is not None and \
        abs(a["lat"] - lat) < 1e-6 and abs(a["lon"] - lon) < 1e-6


def event_anchor(ev: dict, stored: dict | None) -> dict:
    """The location the cascade works from: the event's own location, unless the event
    already sits on our stored best site (then the pre-impact location stored then)."""
    if stored and stored.get("origin") and _close(stored.get("best"), ev.get("lat"),
                                                  ev.get("lon")):
        return dict(stored["origin"])
    return {"lat": ev.get("lat"), "lon": ev.get("lon"), "precision": ev.get("precision"),
            "country": (ev.get("country") or "").upper() or None,
            "place_name": ev.get("place_name")}


def lake_fires(lake):
    """(lat, lon, radius_km, t0, t1) -> FIRMS hotspots [{id, ts, lat, lon, frp}] from the
    observations lake (empty when FIRMS has no data, e.g. no FIRMS_MAP_KEY)."""
    def query(lat: float, lon: float, radius_km: float, t0: datetime, t1: datetime):
        dlat = radius_km / 111.0
        dlon = radius_km / (111.0 * max(0.1, abs(math.cos(math.radians(lat)))))
        rows = lake.query(
            "SELECT id, epoch_us(ts), lat, lon, value FROM observations WHERE type = 'fire' "
            "AND ts >= ? AND ts <= ? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
            "LIMIT 5000", [t0, t1, lat - dlat, lat + dlat, lon - dlon, lon + dlon],
            start=t0, end=t1)
        out = []
        for oid, us, la, lo, frp in rows:
            d = haversine_km(lat, lon, la, lo)
            if d <= radius_km:
                out.append({"id": oid, "ts": utc_iso(datetime.fromtimestamp(us / 1e6, UTC)),
                            "lat": la, "lon": lo, "frp": frp, "distance_km": round(d, 2)})
        return out
    return query


class ImpactJob:
    def __init__(self, event_store, extraction_store, cfg: ImpactConfig, *,
                 resolver: ImpactResolver, usgs: Usgs | None = None, fires=None,
                 clock=utcnow) -> None:
        self.event_store, self.extraction_store, self.cfg = event_store, extraction_store, cfg
        self.resolver, self.usgs, self.fires, self.clock = resolver, usgs, fires, clock
        self.passes = 0
        self.processed = 0
        self.queue_size = 0
        self.last_run_at: str | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------- selection

    def candidates(self, now: datetime) -> list[dict]:
        """Work items {event, sites, quakes, fires_corr, stored, anchor, fp}, oldest
        record / never-computed first, at most max_events_per_pass."""
        cfg = self.cfg
        events = self.event_store.list_events(
            since=now - timedelta(hours=cfg.event_age_h), limit=cfg.select_limit,
            slim=False, include_unlocated=False, origin="news,sensor",
            category=",".join(cfg.categories))
        if not events:
            self.queue_size = 0
            return []
        ids = [e["id"] for e in events]
        recs = self.extraction_store.get_many(ids) if self.extraction_store else {}
        corr = self.event_store.corroboration_rows(ids)
        prints = self.event_store.impact_fingerprints(ids)
        due: list[tuple[tuple, dict]] = []
        for e in events:
            rec = recs.get(e["id"]) or {}
            result = rec.get("result") or {}
            sites = []
            if rec.get("status") == "done" and rec.get("verdict") != "unsupported":
                sites = [s for s in result.get("impact_sites") or [] if isinstance(s, dict)]
            rows = corr.get(e["id"]) or []
            quakes = sorted(r["ref_id"] for r in rows if r["kind"] == "quake")
            fires = sorted(r["ref_id"] for r in rows if r["kind"] == "fire")
            text = " ".join(str(x or "") for x in (e.get("title"), e.get("subcategory"),
                                                   result.get("subtype"),
                                                   result.get("summary")))
            is_fire = bool(fires) or (e.get("category") == "disaster"
                                      and bool(FIRE_WORDS.search(text)))
            is_quake = bool(quakes) and (e.get("category") == "disaster"
                                         or e.get("origin") == "sensor"
                                         or bool(QUAKE_WORDS.search(text)))
            if not sites and not is_fire and not is_quake and e["id"] not in prints:
                continue  # nothing to locate (a stored record is recomputed to empty)
            prev = prints.get(e["id"])
            stored = self.event_store.impacts([e["id"]]).get(e["id"]) if prev else None
            anchor = event_anchor(e, stored)
            fp = hashlib.sha1(json.dumps(
                [IMPACT_VERSION, [round(anchor.get("lat") or 0, 4),
                                  round(anchor.get("lon") or 0, 4), anchor.get("precision")],
                 [[s.get("name"), s.get("kind"), s.get("context"), s.get("lat"),
                   s.get("lon")] for s in sites], quakes, fires, is_fire, is_quake],
                sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
            fresh = (_parse(e.get("updated_at")) or now) >= now - timedelta(
                hours=cfg.refresh_event_age_h)
            if prev is not None and prev[0] == IMPACT_VERSION and prev[1] == fp:
                computed = _parse(prev[2]) or now
                if not (fresh and (is_fire or is_quake)
                        and computed <= now - timedelta(hours=cfg.refresh_h)):
                    continue
            order = (prev is not None, prev[2] if prev else "")
            due.append((order, {"event": e, "sites": sites, "quakes": quakes,
                                "fires_corr": fires, "is_fire": is_fire,
                                "is_quake": is_quake, "anchor": anchor, "fp": fp}))
        due.sort(key=lambda x: x[0])
        self.queue_size = len(due)
        return [w for _, w in due[: cfg.max_events_per_pass]]

    # ------------------------------------------------------------- one event

    async def _fires_near(self, lat, lon, radius_km, t0, t1) -> list[dict]:
        if self.fires is None:
            return []
        try:
            return await asyncio.to_thread(self.fires, lat, lon, radius_km, t0, t1)
        except Exception:
            log.debug("FIRMS lookup failed", exc_info=True)
            return []

    async def compute(self, work: dict, now: datetime) -> dict:
        ev, anchor, cfg = work["event"], work["anchor"], self.cfg
        t0 = _parse(ev.get("t_start")) or now
        t1 = _parse(ev.get("t_end")) or t0
        sites = []
        for s in work["sites"]:
            r = await self.resolver.resolve(s, anchor)
            if r.get("lat") is None:
                continue
            if r["precision"] in FINE and ev.get("category") in (
                    "armed_conflict", "infrastructure", "disaster"):
                hs = await self._fires_near(
                    r["lat"], r["lon"], cfg.firms_snap_km,
                    t0 - timedelta(hours=cfg.firms_snap_h),
                    t1 + timedelta(hours=cfg.firms_snap_h))
                if hs:
                    nearest = min(hs, key=lambda h: h["distance_km"])
                    r["confidence"] = round(min(0.95, (r.get("confidence") or 0) + 0.1), 2)
                    r["firms"] = {"count": len(hs), "nearest": nearest}
            sites.append(r)
        areas = []
        for s in sites:
            a = site_area(s)
            if a is not None and (s["precision"] in FINE or not any(
                    x["precision"] in FINE for x in sites)):
                areas.append(a)
        if work["is_quake"]:
            qa = await self._quake_area(work, ev)
            if qa is not None:
                areas.append(qa)
        if work["is_fire"]:
            center = best_site(sites) or anchor
            if center.get("lat") is not None:
                hs = await self._fires_near(center["lat"], center["lon"], cfg.fire_area_km,
                                            t0 - timedelta(hours=12), t1 + timedelta(hours=24))
                if hs:
                    pts = [(h["lon"], h["lat"]) for h in hs[:2000]]
                    areas.append({"kind": "firms_hull",
                                  "basis": f"FIRMS hotspot hull ({len(hs)} detections, "
                                           f"{cfg.fire_buffer_km:g} km footprint buffer)",
                                  "count": len(hs),
                                  "geometry": buffered_hull(pts, cfg.fire_buffer_km)})
        best = best_site(sites)
        boxes = [geometry_bounds(a["geometry"]) for a in areas]
        boxes += [(s["lon"], s["lat"], s["lon"], s["lat"]) for s in sites]
        return {"event_id": ev["id"], "version": IMPACT_VERSION, "sites": sites,
                "best": None if best is None else {
                    k: best.get(k) for k in ("name", "kind", "lat", "lon", "precision",
                                             "confidence", "resolver", "source", "quote")},
                "areas": areas, "origin": anchor, "input_fp": work["fp"],
                "bbox": union_bounds(boxes), "computed_at": utc_iso(now)}

    async def _quake_area(self, work: dict, ev: dict) -> dict | None:
        usgs_id = work["quakes"][0].split(":", 1)[-1] if work["quakes"] else ""
        data = None
        if self.usgs is not None and usgs_id:
            try:
                data = await self.usgs.shakemap(usgs_id)
            except Exception:
                log.debug("USGS shakemap failed for %s", usgs_id, exc_info=True)
        if data and data.get("mmi_contours"):
            geom = mmi_area(data["mmi_contours"])
            if geom is not None:
                level = round(geom.pop("mmi"))
                return {"kind": "shakemap", "mmi": level, "usgs_id": usgs_id,
                        "basis": f"USGS ShakeMap MMI≥{ROMAN.get(level, level)}",
                        "geometry": geom}
        mag = (data or {}).get("mag")
        if isinstance(mag, int | float) and mag >= self.cfg.felt_min_mag \
                and ev.get("lat") is not None:
            r = felt_radius_km(float(mag))
            return {"kind": "felt_radius", "radius_km": r, "usgs_id": usgs_id or None,
                    "basis": f"felt-radius estimate from magnitude M{mag:.1f} "
                             f"(approx. MMI≥V, {r:g} km; no ShakeMap)",
                    "center": [ev["lon"], ev["lat"]],
                    "geometry": circle(ev["lat"], ev["lon"], r, n=64)}
        return None

    # ------------------------------------------------------------- loop

    async def run_once(self) -> int:
        now = self.clock()
        work = await asyncio.to_thread(self.candidates, now)
        rows = []
        for w in work:
            try:
                rows.append(await self.compute(w, now))
            except Exception:
                log.exception("impact computation failed for %s", w["event"]["id"])
        if rows:
            await asyncio.to_thread(self.event_store.save_impacts, rows)
        self.processed += len(rows)
        self.last_run_at = utc_iso(now)
        return len(rows)

    async def run(self) -> None:
        if not self.cfg.enabled:
            log.info("impact sites disabled (config/impact.yaml)")
            return
        await asyncio.sleep(self.cfg.initial_delay_s)
        while True:
            try:
                await self.run_once()
                self.last_error = None
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("impact pass failed")
            self.passes += 1
            await asyncio.sleep(self.cfg.interval_s)

    def status(self) -> dict:
        nom = self.resolver.nominatim
        return {"enabled": self.cfg.enabled, "passes": self.passes,
                "processed": self.processed, "queue_size": self.queue_size,
                "last_run_at": self.last_run_at, "last_error": self.last_error,
                "nominatim": None if nom is None else {
                    "requests": nom.requests, "cache_hits": nom.cache_hits,
                    "last_error": nom.last_error}}


# ------------------------------------------------------------------- wiring


def build_job(runtime, http: httpx.AsyncClient | None = None) -> ImpactJob:
    settings = runtime.settings
    cfg = ImpactConfig.load(settings.config_dir / "impact.yaml")
    from osint.enrich.gazetteer import CountryCities
    from osint.pipeline.extract.job import get_store
    from osint.pipeline.extract.verdict import gazetteer_geocoder

    cache = HttpCache(settings.data_dir / "geocode_cache.sqlite")
    http = http or httpx.AsyncClient(follow_redirects=True, timeout=30.0)
    gz = getattr(getattr(runtime, "triage", None), "gazetteer", None)
    geocode = gazetteer_geocoder(gz, CountryCities(settings.data_dir / "gazetteer")) \
        if gz is not None else None
    resolver = ImpactResolver(
        pois=ReferencePOIs(), geocode=geocode,
        nominatim=Nominatim(http, cache, enabled=cfg.nominatim_enabled,
                            daily_cap=cfg.nominatim_daily_cap))
    return ImpactJob(runtime.store, get_store(runtime), cfg, resolver=resolver,
                     usgs=Usgs(http, cache, enabled=cfg.usgs_enabled),
                     fires=lake_fires(runtime.lake))


def start(runtime) -> asyncio.Task:
    """Create the job (as runtime.impact_job) and start its loop as a task."""
    job = build_job(runtime, http=getattr(runtime, "http", None))  # closed by Runtime.stop
    runtime.impact_job = job
    return asyncio.create_task(job.run(), name="impact")


