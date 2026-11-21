"""Pure data functions behind the JSON and KML layer endpoints."""

import json
from datetime import UTC, datetime, timedelta

from osint.core.geo import utcnow
from osint.runtime import Runtime

LAYER_KIND = {"aircraft": "air", "ships": "sea"}
MAX_AGE = {"aircraft": timedelta(minutes=30), "ships": timedelta(hours=6)}
OBS_LIMIT = 50000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

BBox = tuple[float, float, float, float]


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


def envelope(layer: str, items: list[dict]) -> dict:
    return {"layer": layer, "generated_at": utcnow().isoformat(), "count": len(items),
            "items": items}


def track_layer_items(runtime: Runtime, layer: str, bbox: BBox | None = None) -> list[dict]:
    kind = LAYER_KIND[layer]
    cutoff = utcnow() - MAX_AGE[layer]
    positions = [
        p for p in list(runtime.ingestor.live.values())
        if p.kind == kind and p.ts >= cutoff and _in_bbox(p.lat, p.lon, bbox)
    ]
    positions.sort(key=lambda p: p.ts, reverse=True)
    return position_items(runtime, positions)


def position_items(runtime: Runtime, positions: list, tracks: dict | None = None) -> list[dict]:
    if tracks is None:
        tracks = runtime.store.get_tracks([p.track_id for p in positions])
    items = []
    for p in positions:
        t = tracks.get(p.track_id) or {}
        items.append({
            "id": p.track_id, "lat": round(p.lat, 5), "lon": round(p.lon, 5),
            "alt_ft": p.alt_ft, "speed_kts": p.speed_kts, "heading_deg": p.heading_deg,
            "ts": p.ts.isoformat(), "name": t.get("name"), "type_code": t.get("type_code"),
            "operator": t.get("operator"), "category": t.get("category"),
            "is_mil": bool(t.get("is_mil")), "high_priority": bool(t.get("high_priority")),
            "extra": t.get("extra") or {},
        })
    return items


def _attrs(raw: str | None) -> dict:
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _obs_rows(
    runtime: Runtime, typ: str, hours: int, dedupe: bool, until: datetime | None = None
) -> list[tuple]:
    end = until or utcnow()
    since = end - timedelta(hours=hours)
    qualify = (
        "QUALIFY row_number() OVER (PARTITION BY id ORDER BY version DESC) = 1 " if dedupe else ""
    )
    sql = (
        "SELECT id, epoch_us(ts), lat, lon, value, attrs FROM observations "
        f"WHERE type = ? AND ts >= ? AND ts <= ? {qualify}ORDER BY ts DESC LIMIT {OBS_LIMIT}"
    )
    return runtime.lake.query(sql, [typ, since, end])


def _iso(us: int) -> str:
    return (_EPOCH + timedelta(microseconds=us)).isoformat()


def fire_items(runtime: Runtime, hours: int = 24, until: datetime | None = None) -> list[dict]:
    out = []
    for id_, us, lat, lon, value, attrs in _obs_rows(runtime, "fire", hours, False, until):
        a = _attrs(attrs)
        out.append({
            "id": id_, "lat": round(lat, 5), "lon": round(lon, 5), "ts": _iso(us), "frp": value,
            "confidence": a.get("confidence"), "satellite": a.get("satellite"),
            "daynight": a.get("daynight"),
        })
    return out


def quake_items(runtime: Runtime, hours: int = 24, until: datetime | None = None) -> list[dict]:
    out = []
    for id_, us, lat, lon, value, attrs in _obs_rows(runtime, "quake", hours, True, until):
        a = _attrs(attrs)
        out.append({
            "id": id_, "lat": round(lat, 5), "lon": round(lon, 5), "ts": _iso(us), "mag": value,
            "depth_km": a.get("depth_km"), "place": a.get("place"),
            "event_type": a.get("event_type"), "url": a.get("url"),
        })
    return out


def status(runtime: Runtime) -> dict:
    sources = {}
    for name, h in runtime.scheduler.health.items():
        sources[name] = {
            "status": h.status,
            "last_success": h.last_success.isoformat() if h.last_success else None,
            "last_error": h.last_error, "last_batch_size": h.last_batch_size,
        }
    live = list(runtime.ingestor.live.values())
    return {"sources": sources,
            "counts": {"aircraft": sum(p.kind == "air" for p in live),
                       "ships": sum(p.kind == "sea" for p in live)}}
