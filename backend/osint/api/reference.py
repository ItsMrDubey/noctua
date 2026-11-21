"""Reference geography endpoints (airports, runways, ports, bases, boundaries, chokepoints)."""

import asyncio

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from osint.api.layers import parse_bbox
from osint.reference.chokepoints import chokepoint_features
from osint.reference.loader import Reference, get_reference

SOURCES = [
    {"id": "ourairports", "name": "OurAirports", "licence": "Public Domain", "url": "https://ourairports.com/data/",
     "attribution": "OurAirports"},
    {"id": "wpi", "name": "NGA World Port Index / Natural Earth ports", "licence": "Public Domain",
     "url": "https://msi.nga.mil/Publications/WPI", "attribution": "NGA WPI / Natural Earth"},
    {"id": "natural-earth", "name": "Natural Earth", "licence": "Public Domain",
     "url": "https://www.naturalearthdata.com/", "attribution": "Natural Earth"},
    {"id": "curated-bases", "name": "Curated military facilities", "licence": "Project-owned",
     "url": None, "attribution": "OSINT curated list"},
    {"id": "chokepoints", "name": "Strategic chokepoints (hand-drawn, approximate)", "licence": "Project-owned",
     "url": None, "attribution": "OSINT"},
]
MAX_RUNWAY_SPAN_DEG = 5.0


def _bbox(raw: str | None):
    if not raw:
        return None
    bb = parse_bbox(raw)
    if bb is None:
        raise HTTPException(422, "invalid bbox, expected w,s,e,n")
    return bb


def _span(bb) -> float:
    w, s, e, n = bb
    return max((e - w) % 360 if w > e else e - w, n - s)


def mount_reference(app: FastAPI, ref: Reference | None = None) -> None:
    getter = (lambda: ref) if ref is not None else get_reference

    def data() -> Reference:
        return getter()

    @app.get("/api/reference/airports")
    async def airports(bbox: str | None = None, min_class: str = Query("medium", pattern="^(large|medium)$"),
                       military_only: bool = False) -> dict:
        r = await asyncio.to_thread(data)
        items = r.query_airports(_bbox(bbox), min_class, military_only)
        return {"items": items, "count": len(items)}

    @app.get("/api/reference/runways")
    async def runways(bbox: str) -> dict:
        bb = _bbox(bbox)
        if _span(bb) > MAX_RUNWAY_SPAN_DEG:
            raise HTTPException(400, f"bbox span must be <= {MAX_RUNWAY_SPAN_DEG} degrees; zoom in")
        r = await asyncio.to_thread(data)
        items = r.query_runways(bb)
        return {"items": items, "count": len(items)}

    @app.get("/api/reference/ports")
    async def ports(bbox: str | None = None) -> dict:
        r = await asyncio.to_thread(data)
        items = r.query_ports(_bbox(bbox))
        return {"items": items, "count": len(items)}

    @app.get("/api/reference/bases")
    async def bases(bbox: str | None = None, type: str | None = None) -> dict:
        r = await asyncio.to_thread(data)
        types = {t for t in (type or "").split(",") if t} or None
        items = r.query_bases(_bbox(bbox), types)
        return {"items": items, "count": len(items)}

    @app.get("/api/reference/boundaries/{layer}")
    async def boundaries(layer: str) -> JSONResponse:
        if layer not in ("land", "disputed", "maritime"):
            raise HTTPException(404, "unknown boundary layer")
        r = await asyncio.to_thread(data)
        return JSONResponse(r.boundaries[layer], headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/reference/chokepoints")
    def chokepoints() -> dict:
        items = chokepoint_features()
        return {"items": items, "count": len(items)}

    @app.get("/api/reference/countries")
    async def countries() -> JSONResponse:
        r = await asyncio.to_thread(data)
        return JSONResponse({"items": r.countries, "count": len(r.countries)},
                            headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/reference/sources")
    def sources() -> dict:
        return {"items": SOURCES}
