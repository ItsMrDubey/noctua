"""Imagery pipeline for one event + the periodic ImageryJob."""

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from osint.imagery import png
from osint.imagery.config import ImageryConfig
from osint.imagery.crop import NoData, crop_scene, fetch_preview
from osint.imagery.stac import Scene, pick_after, pick_before, search_scenes
from osint.imagery.store import ImageryStore
from osint.imagery.tiff import Unsupported

log = logging.getLogger(__name__)
USER_AGENT = "osint-poc/0.1 (imagery)"
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def target_point(event: dict, impact: dict | None = None) -> tuple[float, float, str]:
    """Where to centre the crop: the resolved impact site, else the centroid of a FIRMS
    hotspot hull (the burning facility, often km away from a city-level event location),
    else the event location. Returns (lat, lon, basis)."""
    best = (impact or {}).get("best") or {}
    if best.get("lat") is not None and best.get("lon") is not None:
        return float(best["lat"]), float(best["lon"]), "impact_site"
    for area in (impact or {}).get("areas") or []:
        geom = area.get("geometry") or {}
        if area.get("kind") == "firms_hull" and geom.get("type") == "Polygon":
            ring = (geom.get("coordinates") or [[]])[0]
            pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
            if pts:
                return (sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts),
                        "firms_hull")
    return float(event["lat"]), float(event["lon"]), "event"


def covers(bounds: dict | None, lat: float, lon: float) -> bool:
    """True when a crop's bounds contain the point (previews / no bounds: assume yes)."""
    if not bounds or bounds.get("kind") != "crop" or bounds.get("west") is None:
        return True
    return bounds["south"] <= lat <= bounds["north"] and bounds["west"] <= lon <= bounds["east"]


def needs_work(rows: list[dict], now: datetime, max_attempts: int,
               target: tuple[float, float] | None = None) -> bool:
    """True when the event was never processed, an 'after' wait / failure is due a retry,
    or a stored crop does not cover ``target`` (the event was re-located since)."""
    if not rows:
        return True
    for r in rows:
        if target and r["status"] == "ok" and not covers(r.get("bounds"), *target):
            return True
        due = not r.get("next_try_at") or parse_ts(r["next_try_at"]) <= now
        if r["status"] == "awaiting_after" and due:
            return True
        if r["status"] == "failed" and r["attempts"] < max_attempts and due:
            return True
    return False


def make_client(cfg: ImageryConfig) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True,
        timeout=httpx.Timeout(cfg.read_timeout_s, connect=cfg.connect_timeout_s))


class ImageryProcessor:
    """Synchronous (run in a worker thread) before/after processing of single events."""

    def __init__(self, cfg: ImageryConfig, store: ImageryStore, data_dir: Path) -> None:
        self.cfg, self.store, self.data_dir = cfg, store, data_dir

    def _save(self, event_id: str, role: str, scene: Scene, now: datetime, attempts: int,
              *, client: httpx.Client, lat: float, lon: float, basis: str = "event") -> None:
        cfg = self.cfg
        rel_dir = Path("imagery") / event_id
        (self.data_dir / rel_dir).mkdir(parents=True, exist_ok=True)
        err: str | None = None
        try:
            crop = crop_scene(client, scene, lat, lon, cfg.window_km / 2)
            rel = rel_dir / f"{role}.png"
            (self.data_dir / rel).write_bytes(png.encode_rgb(crop.width, crop.height, crop.rgb))
            bounds = {**crop.bounds, "target": {"lat": lat, "lon": lon, "basis": basis}}
        except (Unsupported, NoData, httpx.HTTPError, ValueError, OSError, KeyError) as exc:
            err = f"full-res crop failed ({type(exc).__name__}: {exc}); preview fallback"
            try:
                data, ext, bounds = fetch_preview(client, scene, lat, lon)
            except (Unsupported, httpx.HTTPError) as exc2:
                self.store.upsert(
                    event_id, role, status="failed", now=now, attempts=attempts + 1,
                    next_try_at=now + timedelta(hours=cfg.retry_after_h), collection=scene.collection,
                    scene_id=scene.id, acquired_at=scene.acquired_at, cloud_pct=scene.cloud_pct,
                    error=f"{err}; preview failed ({type(exc2).__name__}: {exc2})"[:500])
                return
            rel = rel_dir / f"{role}.{ext}"
            (self.data_dir / rel).write_bytes(data)
        self.store.upsert(
            event_id, role, status="ok", now=now, attempts=attempts, collection=scene.collection,
            scene_id=scene.id, acquired_at=scene.acquired_at, cloud_pct=scene.cloud_pct,
            path=rel.as_posix(), bounds=bounds, error=err[:500] if err else None)

    def _score(self, event_id: str) -> None:
        rows = {r["role"]: r for r in self.store.get_assets(event_id)}
        b, a = rows.get("before"), rows.get("after")
        if not (b and a and b["status"] == a["status"] == "ok"):
            return
        if not (b["path"].endswith(".png") and a["path"].endswith(".png")):
            return
        try:
            wb, hb, db = png.decode_rgb((self.data_dir / b["path"]).read_bytes())
            wa, ha, da = png.decode_rgb((self.data_dir / a["path"]).read_bytes())
            if (wb, hb) != (wa, ha):
                return
            self.store.set_change_score(event_id, png.change_score(db, da))
        except (OSError, ValueError):
            log.warning("change score failed for %s", event_id, exc_info=True)

    def _latest_pass(self, client: httpx.Client, lat: float, lon: float, t0: datetime,
                     now: datetime) -> Scene | None:
        """The most recent post-event scene at any cloud cover (why 'after' is waiting)."""
        latest: Scene | None = None
        try:
            for coll in self.cfg.collections:
                for s in search_scenes(client, self.cfg.stac_url, coll, lat, lon, t0, now,
                                       100.0, limit=10):
                    if s.acquired_at >= t0 and (latest is None
                                                or s.acquired_at > latest.acquired_at):
                        latest = s
        except httpx.HTTPError:
            return None
        return latest

    def process(self, event: dict, client: httpx.Client, now: datetime | None = None,
                impact: dict | None = None) -> None:
        now = now or utcnow()
        cfg, eid = self.cfg, event["id"]
        lat, lon, basis = target_point(event, impact if impact is not None
                                       else event.get("_impact"))
        t0 = parse_ts(event["t_start"])
        rows = {r["role"]: r for r in self.store.get_assets(eid)}
        # a crop that misses the (re-located) target is redone, before and after alike
        moved = any(r.get("status") == "ok" and not covers(r.get("bounds"), lat, lon)
                    for r in rows.values())
        todo = [role for role in ("before", "after")
                if (moved and rows.get(role, {}).get("status") == "ok")
                or (rows.get(role, {}).get("status") not in ("ok", "no_scene")
                    and not (rows.get(role, {}).get("status") == "failed"
                             and rows[role]["attempts"] >= cfg.max_attempts))]
        if not todo:
            return
        retry = now + timedelta(hours=cfg.retry_after_h)
        scenes: list[Scene] = []
        try:
            for coll in cfg.collections:
                scenes += search_scenes(client, cfg.stac_url, coll, lat, lon,
                                        t0 - timedelta(days=cfg.lookback_days), now,
                                        cfg.max_cloud_pct)
        except httpx.HTTPError as exc:
            for role in todo:
                prev = rows.get(role, {}).get("attempts", 0)
                self.store.upsert(eid, role, status="failed", now=now, attempts=prev + 1,
                                  next_try_at=retry,
                                  error=f"STAC search failed ({type(exc).__name__}: {exc})"[:500])
            return
        picks = {
            "before": pick_before(scenes, t0, lookback_days=cfg.lookback_days,
                                  max_cloud=cfg.max_cloud_pct),
            "after": pick_after(scenes, t0, now, max_cloud=cfg.max_cloud_pct),
        }
        for role in todo:
            scene = picks[role]
            prev = rows.get(role, {}).get("attempts", 0)
            if scene is None:
                if role == "after":
                    last = self._latest_pass(client, lat, lon, t0, now)
                    self.store.upsert(
                        eid, role, status="awaiting_after", now=now, attempts=prev,
                        next_try_at=retry,
                        collection=last.collection if last else None,
                        scene_id=last.id if last else None,
                        acquired_at=last.acquired_at if last else None,
                        cloud_pct=last.cloud_pct if last else None,
                        error=(f"latest pass {last.acquired_at:%Y-%m-%d} cloud "
                               f"{last.cloud_pct:.0f}% > {cfg.max_cloud_pct:.0f}% limit"
                               if last else "no post-event pass yet"))
                else:
                    self.store.upsert(eid, role, status="no_scene", now=now,
                                      error="no cloud-free scene in lookback window")
                continue
            self._save(eid, role, scene, now, prev, client=client, lat=lat, lon=lon,
                       basis=basis)
        self._score(eid)


class ImageryJob:
    def __init__(self, event_store, store: ImageryStore, cfg: ImageryConfig, data_dir: Path,
                 client_factory=None) -> None:
        self.event_store, self.store, self.cfg = event_store, store, cfg
        self.processor = ImageryProcessor(cfg, store, data_dir)
        self.client_factory = client_factory or (lambda: make_client(cfg))
        self.passes = 0
        self.last_error: str | None = None
        self.last_processed: list[str] = []

    def select(self, now: datetime | None = None) -> list[dict]:
        """Eligible, not-yet-processed (or due for retry) events, newest first, capped."""
        now = now or utcnow()
        cfg = self.cfg
        events = self.event_store.list_events(
            since=now - timedelta(days=cfg.event_age_days), status=",".join(cfg.statuses),
            min_confidence=cfg.min_confidence or None, limit=1000)
        events = [e for e in events if e.get("precision") in cfg.precisions
                  and e.get("lat") is not None and e.get("lon") is not None
                  and e.get("t_start") and EVENT_ID_RE.match(e["id"])]
        state = self.store.assets_for([e["id"] for e in events])
        impacts_of = getattr(self.event_store, "impacts", None)
        impacts = impacts_of([e["id"] for e in events]) if impacts_of else {}
        due = []
        for e in events:
            e["_impact"] = impacts.get(e["id"])
            lat, lon, _ = target_point(e, e["_impact"])
            if needs_work(state.get(e["id"], []), now, cfg.max_attempts, (lat, lon)):
                due.append(e)
        return due[:cfg.max_per_run]

    async def run_once(self) -> list[str]:
        batch = await asyncio.to_thread(self.select)
        sem = asyncio.Semaphore(max(1, self.cfg.concurrency))

        async def one(ev: dict) -> None:
            async with sem:
                def work() -> None:
                    with self.client_factory() as client:
                        self.processor.process(ev, client)
                try:
                    await asyncio.to_thread(work)
                except Exception:
                    log.exception("imagery failed for %s", ev["id"])
                    await asyncio.to_thread(self._record_crash, ev["id"])

        await asyncio.gather(*(one(e) for e in batch))
        self.last_processed = [e["id"] for e in batch]
        return self.last_processed

    def _record_crash(self, event_id: str) -> None:
        now = utcnow()
        rows = {r["role"]: r for r in self.store.get_assets(event_id)}
        for role in ("before", "after"):
            if rows.get(role, {}).get("status") != "ok":
                self.store.upsert(event_id, role, status="failed", now=now,
                                  attempts=rows.get(role, {}).get("attempts", 0) + 1,
                                  next_try_at=now + timedelta(hours=self.cfg.retry_after_h),
                                  error="unexpected error (see server log)")

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
                self.last_error = None
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("imagery pass failed")
            self.passes += 1
            await asyncio.sleep(self.cfg.interval_s)


def get_store(runtime) -> ImageryStore:
    """The runtime's imagery store, created on first use (shared by job and API)."""
    store = getattr(runtime, "imagery_store", None)
    if store is None:
        store = ImageryStore(runtime.settings.data_dir / "imagery.sqlite")
        runtime.imagery_store = store
    return store


def wire(runtime) -> ImageryJob:
    cfg = ImageryConfig.load(runtime.settings.config_dir / "imagery.yaml")
    return ImageryJob(runtime.store, get_store(runtime), cfg, runtime.settings.data_dir)
