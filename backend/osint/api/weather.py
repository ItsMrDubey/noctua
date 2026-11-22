"""Weather routes: cloud cover grid for the 3D cloud layer."""

from fastapi import FastAPI, HTTPException

from osint.api.layers import parse_bbox
from osint.weather import CloudService


def mount_weather(app: FastAPI, service: CloudService | None = None) -> CloudService:
    svc = service or CloudService()

    @app.get("/api/weather/clouds")
    async def clouds(bbox: str) -> dict:
        box = parse_bbox(bbox)
        if box is None:
            raise HTTPException(422, "bbox required: west,south,east,north")
        if (box[3] - box[1]) > 40 or ((box[2] - box[0]) % 360) > 60:
            raise HTTPException(422, "bbox too large for 3D clouds (zoom in)")
        return await svc.clouds(box)

    return svc
