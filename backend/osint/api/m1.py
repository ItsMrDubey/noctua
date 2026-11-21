"""M1 routes: layer JSON, status, KML for Google Earth Pro, static frontend."""

import asyncio
import os

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from osint.api import kml, layers
from osint.config import ROOT
from osint.runtime import Runtime

FRONTEND = ROOT / "frontend"
Hours = Query(24, ge=1, le=168)


def _kml(body: str) -> Response:
    return Response(body, media_type=kml.KML_MEDIA)


def mount_m1(app: FastAPI, runtime: Runtime) -> None:
    @app.get("/api/config")
    def config() -> dict:
        return {"cesium_ion_token": os.environ.get("CESIUM_ION_TOKEN") or None}

    @app.get("/api/layers/aircraft")
    def aircraft(bbox: str | None = None) -> dict:
        return layers.envelope(
            "aircraft", layers.track_layer_items(runtime, "aircraft", layers.parse_bbox(bbox)))

    @app.get("/api/layers/ships")
    def ships(bbox: str | None = None) -> dict:
        return layers.envelope(
            "ships", layers.track_layer_items(runtime, "ships", layers.parse_bbox(bbox)))

    @app.get("/api/layers/fires")
    async def fires(hours: int = Hours) -> dict:
        return layers.envelope("fires", await asyncio.to_thread(layers.fire_items, runtime, hours))

    @app.get("/api/layers/quakes")
    async def quakes(hours: int = Hours) -> dict:
        return layers.envelope(
            "quakes", await asyncio.to_thread(layers.quake_items, runtime, hours))

    @app.get("/api/status")
    def status() -> dict:
        return layers.status(runtime)

    @app.get("/kml/master.kml")
    def master(request: Request) -> Response:
        return _kml(kml.master_kml(str(request.base_url)))

    @app.get("/kml/aircraft.kml")
    def aircraft_kml() -> Response:
        return _kml(kml.aircraft_kml(layers.track_layer_items(runtime, "aircraft")))

    @app.get("/kml/ships.kml")
    def ships_kml() -> Response:
        return _kml(kml.ships_kml(layers.track_layer_items(runtime, "ships")))

    @app.get("/kml/fires.kml")
    async def fires_kml() -> Response:
        return _kml(kml.fires_kml(await asyncio.to_thread(layers.fire_items, runtime, 24)))

    @app.get("/kml/quakes.kml")
    async def quakes_kml() -> Response:
        return _kml(kml.quakes_kml(await asyncio.to_thread(layers.quake_items, runtime, 24)))

    @app.get("/")
    def index():
        index_file = FRONTEND / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
        return {"service": "osint", "frontend": "not built yet", "docs": "/docs"}

    app.mount("/static", StaticFiles(directory=FRONTEND, check_dir=False), name="static")
