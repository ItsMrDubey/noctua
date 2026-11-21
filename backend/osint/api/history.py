"""History endpoints: trails, snapshots (time-slider playback) and H3 density."""

import asyncio
from datetime import UTC, datetime, timedelta

import h3
from fastapi import FastAPI, HTTPException, Query

from osint.api import layers
from osint.core.geo import utcnow
from osint.core.models import Position
from osint.runtime import Runtime

MAX_POINTS = 60
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


def mount(app: FastAPI, runtime: Runtime) -> None:
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
