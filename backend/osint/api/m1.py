"""M1 routes: layer JSON, status, KML for Google Earth Pro, static frontend."""

import asyncio
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response

from osint.api import history, kml, layers, live_ws, static
from osint.api.backfill import mount_backfill
from osint.api.events import mount_events
from osint.api.extraction import mount_extraction
from osint.api.imagery import mount_imagery
from osint.api.movements import mount_movements
from osint.api.nuclear import mount_nuclear
from osint.api.reference import mount_reference
from osint.api.security import is_loopback
from osint.api.weather import mount_weather
from osint.api.webcams import mount_webcams
from osint.config import ROOT
from osint.runtime import Runtime

FRONTEND = ROOT / "frontend"
Hours = Query(24, ge=1, le=168)


def _kml(body: str) -> Response:
    return Response(body, media_type=kml.KML_MEDIA)


def _until(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return history.parse_iso(raw)
    except ValueError:
        raise HTTPException(422, "invalid until") from None


def _json(body: bytes) -> Response:
    return Response(body, media_type="application/json")


def mount_m1(app: FastAPI, runtime: Runtime) -> None:
    cache = layers.LayerSnapshotCache(runtime)
    app.state.layer_cache = cache

    @app.get("/api/config")
    def config(request: Request) -> dict:
        # with an API token the middleware has already authenticated the caller; without one only
        # loopback clients get the ion token (a proxied/tunnelled client must not)
        host = request.client.host if request.client else ""
        open_ok = bool(runtime.settings.api_token) or is_loopback(host)
        return {"cesium_ion_token": (runtime.settings.cesium_ion_token or None) if open_ok else None}

    @app.get("/api/layers/aircraft")
    async def aircraft(bbox: str | None = None) -> Response:
        return _json(await cache.body("aircraft", layers.parse_bbox(bbox)))

    @app.get("/api/layers/ships")
    async def ships(bbox: str | None = None) -> Response:
        return _json(await cache.body("ships", layers.parse_bbox(bbox)))

    @app.get("/api/track/{track_id}")
    async def track(track_id: str) -> dict:
        d = await asyncio.to_thread(layers.track_detail, runtime, track_id)
        if d is None:
            raise HTTPException(404, "unknown track")
        return d

    @app.get("/api/layers/fires")
    async def fires(hours: int = Hours, until: str | None = None) -> Response:
        env = await asyncio.to_thread(layers.obs_layer, runtime, "fires", hours, _until(until))
        return _json(layers.encode(env))

    @app.get("/api/layers/quakes")
    async def quakes(hours: int = Hours, until: str | None = None) -> Response:
        env = await asyncio.to_thread(layers.obs_layer, runtime, "quakes", hours, _until(until))
        return _json(layers.encode(env))

    @app.get("/api/status")
    def status() -> dict:
        return layers.status(runtime)

    @app.get("/kml/master.kml")
    def master(request: Request) -> Response:
        return _kml(kml.master_kml(str(request.base_url), runtime.settings.api_token or None))

    @app.get("/kml/aircraft.kml")
    async def aircraft_kml() -> Response:
        items = (await cache.get("aircraft")).items
        return _kml(await asyncio.to_thread(kml.aircraft_kml, items))

    @app.get("/kml/ships.kml")
    async def ships_kml() -> Response:
        items = (await cache.get("ships")).items
        return _kml(await asyncio.to_thread(kml.ships_kml, items))

    @app.get("/kml/fires.kml")
    async def fires_kml() -> Response:
        return _kml(kml.fires_kml(await asyncio.to_thread(layers.fire_items, runtime, 24)))

    @app.get("/kml/quakes.kml")
    async def quakes_kml() -> Response:
        return _kml(kml.quakes_kml(await asyncio.to_thread(layers.quake_items, runtime, 24)))

    history.mount(app, runtime)
    live_ws.mount(app, runtime, cache.meta)
    mount_reference(app)
    mount_nuclear(app)
    mount_movements(app, runtime)
    mount_events(app, runtime)
    mount_imagery(app, runtime)
    mount_extraction(app, runtime)
    mount_webcams(app)
    mount_weather(app)
    mount_backfill(app, runtime)
    __import__("osint.api.naval", fromlist=["mount_naval"]).mount_naval(app, runtime)
    __import__("osint.api.benchmark", fromlist=["mount_benchmark"]).mount_benchmark(app, runtime)

    @app.get("/")
    def index():
        resp = static.index_response(FRONTEND)
        if resp is not None:
            return resp
        return {"service": "osint", "frontend": "not built yet", "docs": "/docs"}

    static.mount_static(app, FRONTEND)
