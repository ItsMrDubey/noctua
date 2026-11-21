"""Public webcams endpoints. Only catalogue cameras (openly published by their owners) are ever proxied."""

from fastapi import FastAPI, HTTPException, Query, Response

from osint.api.layers import parse_bbox
from osint.webcams.catalogue import ID_RE, Catalogue, SnapshotError, get_catalogue


def mount_webcams(app: FastAPI, cat: Catalogue | None = None) -> None:
    def c() -> Catalogue:
        return cat if cat is not None else get_catalogue()

    @app.get("/api/webcams")
    async def webcams(bbox: str | None = None, source: str | None = None,
                      limit: int = Query(5000, ge=1, le=20000)) -> dict:
        bb = parse_bbox(bbox)
        if bbox and bb is None:
            raise HTTPException(422, "invalid bbox, expected w,s,e,n")
        k = c()
        await k.ensure_fresh()
        srcs = {s for s in (source or "").split(",") if s} or None
        items = [w.slim() for w in k.query(bb, srcs, limit)]
        return {"items": items, "count": len(items)}

    @app.get("/api/webcams/near")
    async def near(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180),
                   radius_km: float = Query(25, gt=0, le=500), limit: int = Query(10, ge=1, le=100)) -> dict:
        k = c()
        await k.ensure_fresh()
        items = k.near(lat, lon, radius_km, limit)
        return {"items": items, "count": len(items)}

    @app.get("/api/webcams/sources")
    async def sources() -> dict:
        k = c()
        await k.ensure_fresh()
        return {"items": k.sources_info(), "total": len(k.items)}

    @app.get("/api/webcams/{cam_id}/snapshot")
    async def snapshot(cam_id: str) -> Response:
        try:
            body, ctype, cam = await c().snapshot(cam_id)
        except SnapshotError as e:
            raise HTTPException(e.status, e.msg) from None
        return Response(body, media_type=ctype, headers={
            "Cache-Control": "max-age=30", "X-Webcam-Source": cam.source,
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Webcam-Attribution": cam.attribution.encode("ascii", "replace").decode(),
            "X-Webcam-Licence": cam.licence.encode("ascii", "replace").decode()})

    @app.get("/api/webcams/{cam_id}")
    async def detail(cam_id: str) -> dict:
        if not ID_RE.match(cam_id):
            raise HTTPException(422, "invalid camera id")
        k = c()
        await k.ensure_fresh()
        cam = k.items.get(cam_id)
        if cam is None:
            raise HTTPException(404, "unknown camera")
        return cam.model_dump()
