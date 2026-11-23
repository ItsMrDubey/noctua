"""Fire conflict-score endpoints: per-hotspot score detail, job status and the cached
DeepStateMap front line. Read-only: the worker's FireScoreJob computes and fetches."""

import asyncio
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from osint.api.layers import encode
from osint.fires import frontline
from osint.fires.job import STATUS_KEY
from osint.fires.store import get_score_store
from osint.runtime import Runtime


def mount_fires(app: FastAPI, runtime: Runtime) -> None:
    ref_dir = runtime.settings.data_dir / "reference"
    cache: dict = {"mtime": None, "body": None}

    def frontline_body() -> bytes:
        try:
            mt = os.stat(ref_dir / frontline.CACHE_NAME).st_mtime
        except OSError:
            mt = None
        if cache["body"] is None or cache["mtime"] != mt:
            cache["body"] = encode(frontline.geojson(frontline.load(ref_dir)))
            cache["mtime"] = mt
        return cache["body"]

    @app.get("/api/reference/frontline")
    async def frontline_geojson() -> Response:
        return Response(await asyncio.to_thread(frontline_body), media_type="application/json")

    @app.get("/api/fires/score/{fire_id}")
    async def fire_score(fire_id: str) -> dict:
        row = await asyncio.to_thread(get_score_store(runtime).get, fire_id)
        if row is None:
            raise HTTPException(404, "hotspot not scored")
        return row

    @app.get("/api/fires/score-status")
    async def fire_score_status() -> dict:
        st = await asyncio.to_thread(get_score_store(runtime).get_meta, STATUS_KEY)
        return st or {"last_run_at": None}
