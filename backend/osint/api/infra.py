"""Critical infrastructure layers and watchlist hits.

GET /api/infra/summary                 datasets (counts, age, licence) + watch job status
GET /api/infra/cables                  submarine cables (lines) + landing points
GET /api/infra/pipelines               oil / gas pipelines (simplified lines)
GET /api/infra/points/{layer}          power | refineries | lng | dams | ground_stations | landings
GET /api/infra/airfields?bbox=&type=   heliports / small airports / seaplane bases (viewport)
GET /api/watch/active                  live tracks currently matching a watchlist
GET /api/watch/alerts?since_id=        watchlist / proximity alert events (newest first)
GET /api/watch/list                    user watchlist entries + sanctions index size

Large layers are encoded and gzip-compressed once per dataset load.
"""

import asyncio
import gzip
import threading
from pathlib import Path

import msgspec
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response

from osint.api.layers import parse_bbox
from osint.infra import datasets
from osint.infra.job import WatchStore, load_config, store_path
from osint.infra.loader import AIRFIELD_TYPES, POINT_LAYERS, Infra, get_infra
from osint.infra.watchlist import UserWatchlist

_ENC = msgspec.json.Encoder()


class _Bodies:
    """layer -> (raw, gzip) bodies, rebuilt when the Infra instance changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: int | None = None
        self._b: dict[str, tuple[bytes, bytes]] = {}

    def get(self, infra: Infra, key: str, build) -> tuple[bytes, bytes]:
        with self._lock:
            if self._owner != id(infra):
                self._owner, self._b = id(infra), {}
            hit = self._b.get(key)
        if hit is not None:
            return hit
        raw = _ENC.encode(build())
        out = (raw, gzip.compress(raw, 6))
        with self._lock:
            self._b[key] = out
        return out


def _send(request: Request, bodies: tuple[bytes, bytes]) -> Response:
    headers = {"Cache-Control": "max-age=3600", "Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(bodies[1], media_type="application/json",
                        headers={**headers, "Content-Encoding": "gzip"})
    return Response(bodies[0], media_type="application/json", headers=headers)


def sources() -> list[dict]:
    return [{"id": d.id, "name": d.name, "licence": d.licence, "url": d.url,
             "attribution": d.attribution, "commercial": d.commercial, "notes": d.notes}
            for d in datasets.DATASETS.values()]


def mount_infra(app: FastAPI, runtime=None, infra: Infra | None = None,
                watch_path: Path | None = None) -> None:
    getter = (lambda: infra) if infra is not None else get_infra
    bodies = _Bodies()
    ws: dict[str, WatchStore | None] = {"store": None}
    ws_lock = threading.Lock()

    def wpath() -> Path | None:
        if watch_path is not None:
            return watch_path
        return store_path(runtime.settings.data_dir) if runtime is not None else None

    def watch() -> WatchStore | None:
        with ws_lock:
            if ws["store"] is None:
                p = wpath()
                if p is None or not p.is_file():
                    return None
                ws["store"] = WatchStore(p, read_only=True)
            return ws["store"]

    def ref_dir(r: Infra) -> Path:
        return r.dir

    @app.get("/api/infra/summary")
    async def summary() -> dict:
        r = await asyncio.to_thread(getter)
        layers = await asyncio.to_thread(r.summary)
        meta = datasets.read_meta(ref_dir(r))
        ds = {}
        for d in datasets.DATASETS.values():
            age = datasets.age_days(ref_dir(r), d)
            ds[d.id] = {"name": d.name, "age_days": None if age is None else round(age, 1),
                        "stale": age is None or age >= d.max_age_days,
                        "max_age_days": d.max_age_days, "fetched": meta.get(d.id)}
        st = watch()
        status = await asyncio.to_thread(st.get_meta, "status") if st else None
        refresh = await asyncio.to_thread(st.get_meta, "refresh") if st else None
        return {"layers": layers, "datasets": ds, "sources": sources(),
                "watch": {"status": status, "refresh": refresh}}

    @app.get("/api/infra/cables")
    async def cables(request: Request) -> Response:
        r = await asyncio.to_thread(getter)
        b = await asyncio.to_thread(bodies.get, r, "cables",
                                    lambda: {"cables": r.cables, "landings": r.landings,
                                             "count": len(r.cables)})
        return _send(request, b)

    @app.get("/api/infra/pipelines")
    async def pipelines(request: Request) -> Response:
        r = await asyncio.to_thread(getter)
        b = await asyncio.to_thread(bodies.get, r, "pipelines",
                                    lambda: {"items": r.pipelines, "count": len(r.pipelines)})
        return _send(request, b)

    @app.get("/api/infra/points/{layer}")
    async def points(layer: str, request: Request) -> Response:
        if layer not in POINT_LAYERS:
            raise HTTPException(404, "unknown layer; one of " + ", ".join(POINT_LAYERS))
        r = await asyncio.to_thread(getter)

        def build() -> dict:
            items = r.points(layer)
            return {"layer": layer, "items": items, "count": len(items)}

        return _send(request, await asyncio.to_thread(bodies.get, r, "pt:" + layer, build))

    @app.get("/api/infra/airfields")
    async def airfields(bbox: str, type: str | None = None, military_only: bool = False,
                        limit: int = Query(4000, ge=1, le=20000)) -> dict:
        bb = parse_bbox(bbox)
        if bb is None:
            raise HTTPException(422, "invalid bbox, expected w,s,e,n")
        types = {t for t in (type or "").split(",") if t} or None
        if types and not types <= set(AIRFIELD_TYPES):
            raise HTTPException(422, "unknown type; one of " + ", ".join(AIRFIELD_TYPES))
        r = await asyncio.to_thread(getter)
        items, trunc = await asyncio.to_thread(r.query_airfields, bb, types, military_only, limit)
        return {"items": items, "count": len(items), "truncated": trunc}

    @app.get("/api/watch/active")
    async def active() -> dict:
        st = watch()
        if st is None:
            return {"items": [], "count": 0, "status": None}
        items = await asyncio.to_thread(st.active)
        status = await asyncio.to_thread(st.get_meta, "status")
        return {"items": items, "count": len(items), "status": status}

    @app.get("/api/watch/alerts")
    async def alerts(since_id: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)) -> dict:
        st = watch()
        if st is None:
            return {"items": [], "count": 0}
        items = await asyncio.to_thread(st.alerts, since_id, limit)
        return {"items": items, "count": len(items)}

    @app.get("/api/watch/list")
    async def watch_list() -> dict:
        cfg_dir = runtime.settings.config_dir if runtime is not None else None
        user = UserWatchlist(cfg_dir / "watchlist.yaml") if cfg_dir else None
        st = watch()
        status = await asyncio.to_thread(st.get_meta, "status") if st else None
        cfg = load_config(cfg_dir / "infra.yaml") if cfg_dir else None
        return {"user": {"entries": user.entries if user else [], "error": user.error if user else None,
                         "path": "config/watchlist.yaml"},
                "index": (status or {}).get("index"),
                "topics": cfg["watchlist"]["topics"] if cfg else None,
                "proximity": cfg["proximity"] if cfg else None,
                "licence": "OpenSanctions data: CC BY-NC 4.0 (non-commercial)", "commercial_ok": False}
