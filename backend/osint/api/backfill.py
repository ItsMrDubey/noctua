"""GET /api/backfill/status: per-source backfill checkpoints (written by `osint backfill`)."""

import asyncio

from fastapi import FastAPI

from osint.backfill.status import backfill_status
from osint.runtime import Runtime


def mount_backfill(app: FastAPI, runtime: Runtime) -> None:
    @app.get("/api/backfill/status")
    async def status() -> dict:
        return await asyncio.to_thread(backfill_status, runtime.store)
