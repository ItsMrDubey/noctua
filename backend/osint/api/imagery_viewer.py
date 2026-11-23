"""Imagery viewer API: scene catalogue for a site, on-demand renders, cached image files.

GET /api/imagery/scenes?lat=&lon=[&days=90 | &from=&to=][&sources=s2,landsat,s1,maxar,wayback]
    every usable free scene over the site, newest first, with per-source status / errors.
GET /api/imagery/render?key=&preset=&lat=&lon=[&km=5][&size=thumb|full][&key2=][&fmt=webp|png]
    renders (or reuses) a window of one scene (two for dNBR / SAR change); the JSON carries
    the signed image ``url``, its lat/lon ``bounds`` (for draping), m/px and the stretch, or
    ``{"error": ...}`` when the scene cannot be rendered there (no data, source down).
GET /api/imagery/view/{name}[?dl=<file name>]
    a rendered image (signed for <img> / Cesium; ``dl`` makes it a download).

Registered before the per-event imagery routes, whose ``/api/imagery/{event_id}`` pattern
would otherwise capture these paths.
"""

import asyncio
import re
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from osint.api.security import sign_path
from osint.imagery.catalog import DEFAULT_SOURCES, SOURCES
from osint.imagery.render import MAX_KM, MIN_KM, PRESETS, RenderError
from osint.imagery.viewer import get_service, parse_when

KEY_RE = re.compile(r"^[a-z0-9]{1,10}:[A-Za-z0-9_./-]{1,200}$")
NAME_RE = re.compile(r"^[0-9a-f]{24}\.(webp|png)$")
DL_RE = re.compile(r"[^A-Za-z0-9_.-]+")
MAX_DAYS = 400


def _site(lat: float, lon: float) -> tuple[float, float]:
    if not (-85 <= lat <= 85 and -180 <= lon <= 180):
        raise HTTPException(422, "lat/lon out of range")
    return lat, lon


def mount_imagery_viewer(app: FastAPI, runtime) -> None:
    token = getattr(runtime.settings, "api_token", "")

    @app.get("/api/imagery/scenes")
    async def scenes(lat: float, lon: float, days: int = Query(90, ge=1, le=MAX_DAYS),
                     start: str | None = Query(None, alias="from"),
                     end: str | None = Query(None, alias="to"), sources: str = "") -> dict:
        _site(lat, lon)
        now = datetime.now(UTC)
        try:
            t1 = parse_when(end, now)
            t0 = parse_when(start, t1 - timedelta(days=days))
        except ValueError as exc:
            raise HTTPException(422, f"bad date: {exc}") from exc
        if t1 <= t0 or (t1 - t0).days > MAX_DAYS:
            raise HTTPException(422, f"window must be 1-{MAX_DAYS} days")
        want = tuple(s for s in (sources.split(",") if sources else DEFAULT_SOURCES) if s in SOURCES)
        svc = get_service(runtime)
        out = await asyncio.to_thread(svc.scenes, lat, lon, t0, t1, want)
        return {**out, "presets": PRESETS}

    @app.get("/api/imagery/render")
    async def render(key: str, preset: str, lat: float, lon: float,
                     km: float = Query(5.0, ge=MIN_KM, le=MAX_KM), size: str = "full",
                     key2: str | None = None, fmt: str = "webp") -> dict:
        _site(lat, lon)
        if not KEY_RE.match(key) or (key2 and not KEY_RE.match(key2)):
            raise HTTPException(422, "bad scene key")
        if size not in ("thumb", "full") or fmt not in ("webp", "png"):
            raise HTTPException(422, "bad size / format")
        svc = get_service(runtime)
        try:
            meta = await asyncio.to_thread(svc.render, key, preset, lat, lon, km, size, key2, fmt)
        # an unrenderable scene / unreachable source is an answer, not a failed request (the
        # viewer shows the reason in place of the image)
        except RenderError as exc:
            return {"error": str(exc)}
        except httpx.HTTPError as exc:
            return {"error": f"imagery source unavailable ({type(exc).__name__}: {exc})"[:300]}
        return {**meta, "url": sign_path(token, f"/api/imagery/view/{meta['file']}")}

    @app.get("/api/imagery/view/{name}")
    async def view(name: str, dl: str | None = None) -> FileResponse:
        if not NAME_RE.match(name):
            raise HTTPException(404, "unknown image")
        path = get_service(runtime).cached(name)
        if path is None:
            raise HTTPException(404, "image expired from the cache; render it again")
        media = "image/png" if name.endswith(".png") else "image/webp"
        headers = {"Cache-Control": "private, max-age=86400"}
        if dl:
            fn = DL_RE.sub("_", dl)[:120] or name
            headers["Content-Disposition"] = f'attachment; filename="{fn}"'
        return FileResponse(path, media_type=media, headers=headers)
