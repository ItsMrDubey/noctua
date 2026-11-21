"""History endpoints: trails, snapshots (time-slider playback) and H3 density."""

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import h3
from fastapi import FastAPI, HTTPException, Query

from osint.api import layers
from osint.core.geo import utcnow
from osint.core.models import Position
from osint.runtime import Runtime

log = logging.getLogger(__name__)
MAX_POINTS = 60
BIN_S = {"hour": 3600, "day": 86400}
COUNT_LAYERS = "^(fires|quakes|events|historical|aircraft|ships)$"
MAX_COUNT_DAYS = {"fires": 800, "quakes": 800, "events": 800, "historical": 800 * 4,
                  "aircraft": 31, "ships": 31}
DENSITY_OBS = {"fires": "fire", "quakes": "quake"}


def parse_iso(raw: str) -> datetime:
    d = datetime.fromisoformat(raw.strip().replace("Z", "+00:00").replace(" ", "+"))
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


def _downsample(points: list, n: int) -> list:
    if len(points) <= n:
        return points
    idx = {round(i * (len(points) - 1) / (n - 1)) for i in range(n)}
    return [points[i] for i in sorted(idx)]


def track_points(runtime: Runtime, track_id: str, hours: int) -> list[dict]:
    since = utcnow() - timedelta(hours=hours)
    rows = runtime.lake.query(
        "SELECT epoch_us(ts), lat, lon, alt_ft, speed_kts, heading_deg FROM positions "
        "WHERE track_id = ? AND ts >= ? ORDER BY ts", [track_id, since], start=since)
    pts = [{"ts": layers._iso(r[0]), "lat": r[1], "lon": r[2], "alt_ft": r[3],
            "speed_kts": r[4], "heading_deg": r[5]} for r in rows]
    live = runtime.ingestor.live.get(track_id)
    if live is not None and live.ts >= since and (not pts or live.ts.isoformat() > pts[-1]["ts"]):
        pts.append({"ts": live.ts.isoformat(), "lat": live.lat, "lon": live.lon,
                    "alt_ft": live.alt_ft, "speed_kts": live.speed_kts,
                    "heading_deg": live.heading_deg})
    return pts


def multi_tracks(runtime: Runtime, kind: str, hours: int, mil_only: bool) -> dict:
    since = utcnow() - timedelta(hours=hours)
    rows = runtime.lake.query(
        "SELECT track_id, epoch_us(ts), lon, lat, alt_ft FROM positions "
        "WHERE kind = ? AND ts >= ? ORDER BY track_id, ts", [kind, since], start=since)
    by: dict[str, list] = {}
    for tid, us, lon, lat, alt in rows:
        by.setdefault(tid, []).append([lon, lat, alt, us / 1e6])
    if mil_only and by:
        info = runtime.store.get_tracks(list(by))
        by = {k: v for k, v in by.items() if (info.get(k) or {}).get("is_mil")}
    return {"tracks": {k: _downsample(v, MAX_POINTS) for k, v in by.items()}}


def snapshot_positions(
    runtime: Runtime, at: datetime, kind: str, window_min: int
) -> list[Position]:
    since = at - timedelta(minutes=window_min)
    rows = runtime.lake.query(
        "SELECT track_id, kind, epoch_us(ts), lat, lon, alt_ft, speed_kts, heading_deg, source "
        "FROM positions WHERE kind = ? AND ts >= ? AND ts <= ? "
        "QUALIFY row_number() OVER (PARTITION BY track_id ORDER BY ts DESC) = 1",
        [kind, since, at], start=since, end=at)
    return [Position(track_id=r[0], kind=r[1], ts=datetime.fromtimestamp(r[2] / 1e6, UTC),
                     lat=r[3], lon=r[4], alt_ft=r[5], speed_kts=r[6], heading_deg=r[7],
                     source=r[8]) for r in rows]


def density(runtime: Runtime, layer: str, res: int, hours: int) -> dict:
    since = utcnow() - timedelta(hours=hours)
    col = "h3_3" if res == 3 else "h3_5"
    if layer in DENSITY_OBS:
        sql = f"SELECT {col}, count(*) FROM observations WHERE type = ? AND ts >= ? GROUP BY 1"
        params = [DENSITY_OBS[layer], since]
    else:
        sql = f"SELECT {col}, count(*) FROM positions WHERE kind = ? AND ts >= ? GROUP BY 1"
        params = [layers.LAYER_KIND[layer], since]
    counts: dict[str, int] = {}
    for cell, n in runtime.lake.query(sql, params, start=since):
        key = h3.int_to_str(cell)
        if res == 4:
            key = h3.cell_to_parent(key, 4)
        counts[key] = counts.get(key, 0) + n
    cells = [{"h3": c, "count": n, "boundary": [list(v) for v in h3.cell_to_boundary(c)]}
             for c, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    return {"res": res, "cells": cells}


CACHE_TTL_S = 6 * 3600   # closed bins are immutable except for late backfills
CHUNK_DAYS = 30
CACHE_WORKERS = 4
_cache: dict[tuple[int, str, int], dict] = {}   # (id(lake), layer, step) -> {at, lo, hi, bins}
_cache_lock = threading.Lock()


def _lake_counts(runtime: Runtime, layer: str, step: int, start: datetime, end: datetime) -> dict[int, int]:
    bucket = f"(epoch_us(ts) // {step * 1_000_000}) * {step}"
    if layer in DENSITY_OBS:
        sql = (f"SELECT {bucket}, count(*) FROM observations WHERE type = ? AND ts >= ? AND ts < ? "
               "GROUP BY 1 ORDER BY 1")
        params = [DENSITY_OBS[layer], start, end]
    else:
        sql = (f"SELECT {bucket}, count(DISTINCT track_id) FROM positions WHERE kind = ? AND ts >= ? "
               "AND ts < ? GROUP BY 1 ORDER BY 1")
        params = [layers.LAYER_KIND[layer], start, end]
    return {int(t): int(n) for t, n in runtime.lake.query(sql, params, start=start, end=end)}


def _lake_counts_parallel(runtime: Runtime, layer: str, step: int, lo: int, hi: int) -> dict[int, int]:
    """Counts for [lo, hi) epoch seconds, in month-sized windows over a few threads (a year is
    ~8k hourly parquet files: opening them dominates, and parallelises)."""
    span = CHUNK_DAYS * 86400 // step * step
    wins = [(a, min(a + span, hi)) for a in range(lo, hi, span)]
    out: dict[int, int] = {}
    def one(w):
        return _lake_counts(runtime, layer, step, datetime.fromtimestamp(w[0], UTC),
                            datetime.fromtimestamp(w[1], UTC))
    if len(wins) == 1:
        return one(wins[0])
    with ThreadPoolExecutor(CACHE_WORKERS) as ex:
        for part in ex.map(one, wins):
            out.update(part)
    return out


def _lake_counts_cached(runtime: Runtime, layer: str, step: int, start: datetime, end: datetime,
                        now: float | None = None) -> list[tuple[int, int]]:
    """Closed bins (ending > 1 h ago) are cached in memory per (layer, bin); only the open tail is
    queried live, so repeat loads of a 1-year histogram are instant."""
    now_s = time.time() if now is None else now
    closed = int((now_s - 3600) // step * step)               # first bin that is not yet closed
    a = int(start.timestamp() // step * step)
    b = int(end.timestamp())
    key = (id(runtime.lake), layer, step)
    bins: dict[int, int] = {}
    if a < closed:
        hi = min(closed, -(-b // step) * step)
        with _cache_lock:
            ent = _cache.get(key)
            if ent and time.monotonic() - ent["at"] > CACHE_TTL_S:
                ent = _cache.pop(key)
                ent = None
            if ent is None:
                ent = _cache[key] = {"at": time.monotonic(), "lo": hi, "hi": hi, "bins": {}}
                missing = [(a, hi)] if a < hi else []
            else:
                missing = ([(a, ent["lo"])] if a < ent["lo"] else []) + ([(ent["hi"], hi)] if hi > ent["hi"] else [])
            for lo_, hi_ in missing:
                ent["bins"].update(_lake_counts_parallel(runtime, layer, step, lo_, hi_))
            if missing:
                ent["lo"] = min(ent["lo"], *(m[0] for m in missing))
                ent["hi"] = max(ent["hi"], *(m[1] for m in missing))
            bins.update({t: n for t, n in ent["bins"].items() if a <= t < hi})
    tail_lo = max(a, closed)
    if tail_lo < b:
        bins.update(_lake_counts(runtime, layer, step, datetime.fromtimestamp(tail_lo, UTC), end))
    return sorted(bins.items())


def history_counts(runtime: Runtime, layer: str, bin_: str, start: datetime, end: datetime) -> dict:
    """Sparse per-bin counts [[bin_start_epoch_ms, n], ...] over [start, end), bins aligned to UTC.
    Lake layers are one DuckDB GROUP BY over the time-pruned partitions; events one SQLite GROUP BY."""
    step = BIN_S[bin_]
    if layer in ("events", "historical"):
        from osint.store.sqlite import HISTORICAL_STATUS, iso

        cmp = "=" if layer == "historical" else "!="
        sql = (f"SELECT CAST(strftime('%s', substr(t_start, 1, 19)) AS INTEGER) / {step} * {step}, count(*) "
               f"FROM events WHERE t_start >= ? AND t_start < ? AND status {cmp} ? "
               + ("" if layer == "historical" else "AND lat IS NOT NULL ") + "GROUP BY 1 ORDER BY 1")
        store = runtime.store
        with store._lock:
            rows = store._db.execute(sql, (iso(start), iso(end), HISTORICAL_STATUS)).fetchall()
        pairs = [(r[0], r[1]) for r in rows if r[0] is not None]
    else:
        pairs = _lake_counts_cached(runtime, layer, step, start, end)
    return {"layer": layer, "bin": bin_, "bin_s": step, "start": start.isoformat(), "end": end.isoformat(),
            "counts": [[t * 1000, n] for t, n in pairs]}


def _warm(runtime: Runtime) -> None:
    """Pre-compute the 1-year day-bin histograms so the first UI load of a long range is instant."""
    try:
        now = utcnow()
        for layer in ("quakes", "fires"):
            history_counts(runtime, layer, "day", now - timedelta(days=365), now)
    except Exception:
        log.warning("history counts warm-up failed", exc_info=True)


def mount(app: FastAPI, runtime: Runtime) -> None:
    timer = threading.Timer(15.0, _warm, args=(runtime,))
    timer.daemon = True
    timer.start()

    @app.get("/api/tracks/{track_id}")
    async def track(track_id: str, hours: int = Query(6, ge=1, le=48)) -> dict:
        pts = await asyncio.to_thread(track_points, runtime, track_id, hours)
        return {"track_id": track_id, "points": pts}

    @app.get("/api/tracks")
    async def tracks(kind: str = Query("air", pattern="^(air|sea)$"),
                     hours: int = Query(1, ge=1, le=48), mil_only: bool = False) -> dict:
        return await asyncio.to_thread(multi_tracks, runtime, kind, hours, mil_only)

    @app.get("/api/snapshot")
    async def snapshot(at: str, kind: str = Query("air", pattern="^(air|sea)$"),
                       window_min: int = Query(10, ge=1, le=240)) -> dict:
        try:
            when = parse_iso(at)
        except ValueError:
            raise HTTPException(422, "invalid at") from None
        pos = await asyncio.to_thread(snapshot_positions, runtime, when, kind, window_min)
        layer = "aircraft" if kind == "air" else "ships"
        items = await asyncio.to_thread(layers.position_items, runtime, pos)
        return layers.envelope(layer, items)

    @app.get("/api/density")
    async def density_route(layer: str = Query(pattern="^(aircraft|ships|fires|quakes)$"),
                            res: int = Query(3, ge=3, le=5),
                            hours: int = Query(24, ge=1, le=168)) -> dict:
        return await asyncio.to_thread(density, runtime, layer, res, hours)

    @app.get("/api/history/counts")
    async def history_counts_route(layer: str = Query(pattern=COUNT_LAYERS),
                                   bin: str = Query("hour", pattern="^(hour|day)$"),
                                   start: str | None = None, end: str | None = None) -> dict:
        try:
            t1 = parse_iso(end) if end else utcnow()
            t0 = parse_iso(start) if start else t1 - timedelta(days=7)
        except ValueError:
            raise HTTPException(422, "invalid start/end") from None
        if t0 >= t1:
            raise HTTPException(422, "start must be before end")
        if t1 - t0 > timedelta(days=MAX_COUNT_DAYS[layer]):
            raise HTTPException(422, f"range too long for {layer} (max {MAX_COUNT_DAYS[layer]} d)")
        return await asyncio.to_thread(history_counts, runtime, layer, bin, t0, t1)
