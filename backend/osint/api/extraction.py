"""Extraction API: job status and the verified facts (with quotes) for one event."""

import asyncio
import re

from fastapi import FastAPI, HTTPException

from osint.pipeline.extract.job import get_store

EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def mount_extraction(app: FastAPI, runtime) -> None:
    store = get_store(runtime)

    @app.get("/api/extraction/status")
    async def extraction_status() -> dict:
        job = getattr(runtime, "extraction_job", None)
        if job is None:
            return {"running": False, "booting": bool(getattr(runtime, "booting", False)),
                    "verdicts": await asyncio.to_thread(store.verdict_counts),
                    "last_errors": await asyncio.to_thread(store.last_errors)}
        return {"running": True, **await asyncio.to_thread(job.status)}

    @app.get("/api/extraction/{event_id}")
    async def extraction(event_id: str) -> dict:
        if not EVENT_ID_RE.match(event_id):
            raise HTTPException(404, "unknown event")
        rec = await asyncio.to_thread(store.get, event_id)
        if rec is None:
            raise HTTPException(404, "not extracted")
        return rec
