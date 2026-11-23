"""Data functions behind the JSON and KML layer endpoints, plus the shared snapshot cache."""

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import msgspec

from osint.core.geo import utcnow
from osint.runtime import LIVE_MAX_AGE, Runtime, is_current, live_cutoffs

log = logging.getLogger(__name__)

LAYER_KIND = {"aircraft": "air", "ships": "sea"}
MAX_AGE = {layer: LIVE_MAX_AGE[kind] for layer, kind in LAYER_KIND.items()}
OBS_LIMIT = 50000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# how often the shared aircraft/ships snapshots are rebuilt at most
SNAPSHOT_TTL_S = {"aircraft": 5.0, "ships": 15.0}
TRACK_META_TTL_S = 600.0
TRACK_META_EMPTY_TTL_S = 60.0  # retry sooner when a track had no metadata yet

BBox = tuple[float, float, float, float]
_ENCODER = msgspec.json.Encoder()
_NO_META = {"name": None, "type_code": None, "operator": None, "category": None, "reg": None,
            "is_mil": False, "high_priority": False}


def parse_bbox(raw: str | None) -> BBox | None:
    if not raw:
        return None
    try:
        w, s, e, n = (float(x) for x in raw.split(","))
    except ValueError:
        return None
    return w, s, e, n


def _in_bbox(lat: float, lon: float, bbox: BBox | None) -> bool:
    if bbox is None:
        return True
    w, s, e, n = bbox
    if not s <= lat <= n:
        return False
    return w <= lon <= e if w <= e else (lon >= w or lon <= e)  # antimeridian wrap


def envelope(layer: str, items: list[dict], truncated: bool | None = None,
             generated_at: str | None = None) -> dict:
    env = {"layer": layer, "generated_at": generated_at or utcnow().isoformat(),
           "count": len(items), "items": items}
    if truncated is not None:
        env["truncated"] = truncated
    return env


def encode(obj: object) -> bytes:
    return _ENCODER.encode(obj)


def slim_item(p, t: dict | None) -> dict:
    """Map list item: position plus the track metadata the UI needs (no ``extra``)."""
    item = {
        "id": p.track_id, "lat": round(p.lat, 5), "lon": round(p.lon, 5),
        "alt_ft": p.alt_ft, "speed_kts": p.speed_kts, "heading_deg": p.heading_deg,
        "ts": p.ts.isoformat(),
    }
    if t:
        get = t.get
        item.update(name=get("name"), type_code=get("type_code"), operator=get("operator"),
                    category=get("category"), reg=get("reg"), is_mil=bool(get("is_mil")),
                    high_priority=bool(get("high_priority")))
    else:
        item.update(_NO_META)
    return item


def position_items(runtime: Runtime, positions: list, tracks: dict | None = None) -> list[dict]:
    if tracks is None:
        tracks = runtime.store.get_tracks([p.track_id for p in positions])
    return [slim_item(p, tracks.get(p.track_id)) for p in positions]


def copy_live(runtime: Runtime) -> list:
    with runtime.ingestor._lock:
        return list(runtime.ingestor.live.values())


def track_layer_items(runtime: Runtime, layer: str, bbox: BBox | None = None) -> list[dict]:
    """Uncached build (for one-off callers; the HTTP layers use LayerSnapshotCache)."""
    kind = LAYER_KIND[layer]
    cutoff = utcnow() - MAX_AGE[layer]
    positions = [p for p in copy_live(runtime)
                 if p.kind == kind and p.ts >= cutoff and _in_bbox(p.lat, p.lon, bbox)]
    positions.sort(key=lambda p: p.ts, reverse=True)
    return position_items(runtime, positions)


def track_detail(runtime: Runtime, track_id: str) -> dict | None:
    """Full track record (incl. ``extra``) plus the latest live position."""
    track = runtime.store.get_tracks([track_id]).get(track_id)
    p = runtime.ingestor.live.get(track_id)
    if track is None and p is None:
        return None
    pos = None
    if p is not None:
        pos = {"ts": p.ts.isoformat(), "lat": p.lat, "lon": p.lon, "alt_ft": p.alt_ft,
               "speed_kts": p.speed_kts, "heading_deg": p.heading_deg, "source": p.source}
    return {"id": track_id, "track": track, "position": pos}


class TrackMetaCache:
    """In-memory track metadata, refreshed incrementally from the SQLite store.

    Only ids that are missing or older than TRACK_META_TTL_S (TRACK_META_EMPTY_TTL_S for
    tracks that had no metadata yet) are fetched with ``get_tracks``.
    """

    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime
        self._meta: dict[str, tuple[dict, float]] = {}
        self._lock = threading.Lock()

    def get_many(self, ids: list[str]) -> dict[str, dict]:
        now = time.monotonic()
        full_cut, empty_cut = now - TRACK_META_TTL_S, now - TRACK_META_EMPTY_TTL_S
        out: dict[str, dict] = {}
        stale = []
        with self._lock:
            meta = self._meta
            for tid in ids:
                entry = meta.get(tid)
                if entry is None or entry[1] < full_cut or (not entry[0] and entry[1] < empty_cut):
                    stale.append(tid)
                else:
                    out[tid] = entry[0]
        if stale:
            fetched = self.rt.store.get_tracks(stale)
            with self._lock:
                for tid in stale:
                    t = fetched.get(tid) or {}
                    self._meta[tid] = (t, now)
                    out[tid] = t
        return out

    def retain(self, ids: set[str]) -> None:
        """Forget tracks that are no longer live."""
        with self._lock:
            if len(self._meta) > len(ids):
                self._meta = {k: v for k, v in self._meta.items() if k in ids}

    def __len__(self) -> int:
        return len(self._meta)


@dataclass
class Snapshot:
    layer: str
    built_at: float
    generated_at: str
    items: list[dict]
    body: bytes  # the full envelope, msgspec-encoded once and served to every client


class LayerSnapshotCache:
    """Shared aircraft/ships layer snapshots, rebuilt at most every SNAPSHOT_TTL_S seconds
    in a worker thread and encoded once."""

    def __init__(self, runtime: Runtime, ttl_s: dict[str, float] | None = None) -> None:
        self.rt = runtime
        self.ttl_s = dict(SNAPSHOT_TTL_S if ttl_s is None else ttl_s)
        self.meta = TrackMetaCache(runtime)
        self._snaps: dict[str, Snapshot] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def build(self, layer: str) -> Snapshot:
        kind = LAYER_KIND[layer]
        live = copy_live(self.rt)
        cutoff = utcnow() - MAX_AGE[layer]
        positions = [p for p in live if p.kind == kind and p.ts >= cutoff]
        positions.sort(key=lambda p: p.ts, reverse=True)
        meta = self.meta.get_many([p.track_id for p in positions])
        self.meta.retain({p.track_id for p in live})
        items = [slim_item(p, meta.get(p.track_id)) for p in positions]
        generated_at = utcnow().isoformat()
        body = encode(envelope(layer, items, generated_at=generated_at))
        return Snapshot(layer, time.monotonic(), generated_at, items, body)

    def _fresh(self, layer: str) -> Snapshot | None:
        snap = self._snaps.get(layer)
        if snap is not None and time.monotonic() - snap.built_at < self.ttl_s[layer]:
            return snap
        return None

    async def get(self, layer: str) -> Snapshot:
        snap = self._fresh(layer)
        if snap is not None:
            return snap
        lock = self._locks.setdefault(layer, asyncio.Lock())
        async with lock:
            snap = self._fresh(layer)
            if snap is None:
                snap = await asyncio.to_thread(self.build, layer)
                self._snaps[layer] = snap
            return snap

    async def body(self, layer: str, bbox: BBox | None = None) -> bytes:
        snap = await self.get(layer)
        if bbox is None:
            return snap.body
        items = [i for i in snap.items if _in_bbox(i["lat"], i["lon"], bbox)]
        return encode(envelope(layer, items, generated_at=snap.generated_at))


def live_counts(runtime: Runtime) -> dict[str, int]:
    """Current live tracks per kind, using the same per-kind max age as the layers."""
    cutoffs = live_cutoffs()
    out: dict[str, int] = {}
    for p in list(runtime.ingestor.live.values()):
        if is_current(p, cutoffs):
            out[p.kind] = out.get(p.kind, 0) + 1
    return out


def _attrs(raw: str | None) -> dict:
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _obs_rows(
    runtime: Runtime, typ: str, hours: int, dedupe: bool, until: datetime | None = None,
    limit: int = OBS_LIMIT,
) -> list[tuple]:
    end = until or utcnow()
    since = end - timedelta(hours=hours)
    qualify = (
        "QUALIFY row_number() OVER (PARTITION BY id ORDER BY version DESC) = 1 " if dedupe else ""
    )
    sql = (
        "SELECT id, epoch_us(ts), lat, lon, value, attrs FROM observations "
        f"WHERE type = ? AND ts >= ? AND ts <= ? {qualify}ORDER BY ts DESC LIMIT {int(limit)}"
    )
    return runtime.lake.query(sql, [typ, since, end], start=since, end=end)


def _iso(us: int) -> str:
    return (_EPOCH + timedelta(microseconds=us)).isoformat()


def fire_scores(runtime: Runtime, hours: int, until: datetime | None) -> dict[str, tuple]:
    """id -> (conflict score, reasons) computed by the worker's FireScoreJob ({} if none)."""
    from osint.fires.store import get_score_store

    end = until or utcnow()
    try:
        return get_score_store(runtime).between(end - timedelta(hours=hours), end)
    except Exception:
        log.exception("fire scores unavailable")
        return {}


def fire_items(runtime: Runtime, hours: int = 24, until: datetime | None = None,
               limit: int = OBS_LIMIT) -> list[dict]:
    out = []
    scores = fire_scores(runtime, hours, until)
    for id_, us, lat, lon, value, attrs in _obs_rows(runtime, "fire", hours, False, until, limit):
        a = _attrs(attrs)
        sc = scores.get(id_)
        out.append({
            "id": id_, "lat": round(lat, 5), "lon": round(lon, 5), "ts": _iso(us), "frp": value,
            "confidence": a.get("confidence"), "satellite": a.get("satellite"),
            "daynight": a.get("daynight"),
            "conflict_score": sc[0] if sc else None,
            "conflict_reasons": [r for r in sc[1].split(",") if r] if sc else [],
        })
    return out


def quake_items(runtime: Runtime, hours: int = 24, until: datetime | None = None,
                limit: int = OBS_LIMIT) -> list[dict]:
    out = []
    for id_, us, lat, lon, value, attrs in _obs_rows(runtime, "quake", hours, True, until, limit):
        a = _attrs(attrs)
        out.append({
            "id": id_, "lat": round(lat, 5), "lon": round(lon, 5), "ts": _iso(us), "mag": value,
            "depth_km": a.get("depth_km"), "place": a.get("place"),
            "event_type": a.get("event_type"), "url": a.get("url"),
        })
    return out


OBS_ITEMS = {"fires": fire_items, "quakes": quake_items}


def obs_layer(runtime: Runtime, layer: str, hours: int = 24,
              until: datetime | None = None) -> dict:
    """Observation layer envelope; queries one extra row to report truncation."""
    limit = OBS_LIMIT
    items = OBS_ITEMS[layer](runtime, hours, until, limit + 1)
    return envelope(layer, items[:limit], truncated=len(items) > limit)


# data layer -> the collector whose polls feed it (age of its last success is shown in the UI)
LAYER_SOURCES = {"aircraft": "adsb", "ships": "ais", "fires": "firms", "quakes": "usgs"}


class LayerVersions:
    """Per observation/event layer a version string that changes as soon as new rows are
    queryable: the ingestor's last lake flush containing that observation type, and the
    event builder's last pass that created or updated events. The live websocket pushes
    changes so the UI refetches at once instead of on its next slow poll."""

    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime
        self._passes: int | None = None
        self._events: str | None = None

    def get(self) -> dict[str, str | None]:
        flushed = getattr(self.rt.ingestor, "flushed", None) or {}
        b = getattr(self.rt, "event_builder", None)
        passes = getattr(b, "passes", None)
        if passes is not None and passes != self._passes:
            self._passes = passes
            last = getattr(b, "last_pass", None)
            if self._events is None or (last is not None and (last.created or last.updated)):
                self._events = str(passes)
        return {"fires": flushed.get("fire"), "quakes": flushed.get("quake"),
                "events": self._events}


def status(runtime: Runtime, versions: LayerVersions | None = None) -> dict:
    sources = {}
    for name, h in runtime.scheduler.health.items():
        sources[name] = {
            "status": h.status,
            "last_success": h.last_success.isoformat() if h.last_success else None,
            "last_error": h.last_error, "last_batch_size": h.last_batch_size,
        }
    counts = live_counts(runtime)
    out = {"sources": sources,
           "counts": {layer: counts.get(kind, 0) for layer, kind in LAYER_KIND.items()}}
    # per layer: collector + its last successful poll, and when new rows last landed
    ver = (versions or LayerVersions(runtime)).get()
    out["layers"] = {
        layer: {"source": src, "polled_at": sources.get(src, {}).get("last_success"),
                "data_at": ver.get(layer)}
        for layer, src in LAYER_SOURCES.items()}
    return out
