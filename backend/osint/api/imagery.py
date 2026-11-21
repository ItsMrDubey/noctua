"""Imagery API: before/after crop metadata and image files for an event."""

import asyncio
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from osint.api.security import sign_path
from osint.imagery.job import EVENT_ID_RE, get_store

NAME_RE = re.compile(r"^(before|after)\.(png|jpg)$")
MEDIA = {"png": "image/png", "jpg": "image/jpeg"}
COPERNICUS = "Contains modified Copernicus Sentinel data {year}"
LANDSAT = "Landsat imagery courtesy of USGS"


def attribution(collection: str | None, acquired_at: str | None) -> str | None:
    if not collection:
        return None
    if collection.startswith("sentinel"):
        return COPERNICUS.format(year=(acquired_at or "")[:4] or "")
    if collection.startswith("landsat"):
        return LANDSAT
    return None


def _valid_id(event_id: str) -> str:
    if not EVENT_ID_RE.match(event_id):
        raise HTTPException(404, "unknown event")
    return event_id


def _asset_out(event_id: str, a: dict, token: str = "") -> dict:
    url = None
    if a.get("path") and a["status"] == "ok":
        url = sign_path(token, f"/api/imagery/{event_id}/{a['role']}.{a['path'].rsplit('.', 1)[-1]}")
    return {
        "role": a["role"], "status": a["status"], "collection": a["collection"],
        "scene_id": a["scene_id"], "acquired_at": a["acquired_at"], "cloud_pct": a["cloud_pct"],
        "change_score": a["change_score"], "url": url, "bounds": a["bounds"],
        "kind": (a["bounds"] or {}).get("kind"), "error": a["error"],
        "attribution": attribution(a["collection"], a["acquired_at"]),
    }


def mount_imagery(app: FastAPI, runtime) -> None:
    store = get_store(runtime)
    root = (runtime.settings.data_dir / "imagery").resolve()

    @app.get("/api/imagery/{event_id}")
    async def imagery(event_id: str) -> dict:
        _valid_id(event_id)
        rows = await asyncio.to_thread(store.get_assets, event_id)
        assets = [_asset_out(event_id, a, getattr(runtime.settings, "api_token", "")) for a in rows]
        attributions = sorted({a["attribution"] for a in assets if a["attribution"]})
        return {"event_id": event_id, "assets": assets, "attributions": attributions}

    @app.get("/api/imagery/{event_id}/{name}")
    async def imagery_file(event_id: str, name: str) -> FileResponse:
        _valid_id(event_id)
        m = NAME_RE.match(name)
        if not m:
            raise HTTPException(404, "unknown image")
        role, ext = m.groups()
        rows = await asyncio.to_thread(store.get_assets, event_id)
        row = next((r for r in rows if r["role"] == role and r["status"] == "ok"
                    and r["path"] and r["path"].endswith("." + ext)), None)
        if row is None:
            raise HTTPException(404, "no such image")
        path = (runtime.settings.data_dir / row["path"]).resolve()
        if not path.is_relative_to(root) or not Path(path).is_file():
            raise HTTPException(404, "no such image")
        return FileResponse(path, media_type=MEDIA[ext],
                            headers={"Cache-Control": "public, max-age=3600"})
