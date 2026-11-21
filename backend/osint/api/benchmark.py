"""Benchmark API: GDELT scorecard, snapshot history, gold-set sample and labels."""

import asyncio
from datetime import timedelta

import msgspec
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from osint.benchmark.job import get_service
from osint.benchmark.sample import build_sample

_ENCODER = msgspec.json.Encoder()


def _json(obj: object) -> Response:
    return Response(_ENCODER.encode(obj), media_type="application/json")


class LabelIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=64)
    correct_event: bool
    correct_location: bool
    notes: str = Field(default="", max_length=2000)


def mount_benchmark(app: FastAPI, runtime) -> None:
    def svc():
        return get_service(runtime)

    def hours(window: str) -> float:
        try:
            return svc().cfg.window_hours(window)
        except KeyError:
            raise HTTPException(
                422, f"window must be one of {sorted(svc().cfg.windows)}") from None

    @app.get("/api/benchmark/scorecard")
    async def scorecard(window: str = "24h") -> Response:
        hours(window)
        return _json(await svc().scorecard(window))

    @app.get("/api/benchmark/history")
    async def history(metric: str | None = None, days: float = Query(7, gt=0, le=90),
                      window: str = "24h") -> Response:
        hours(window)
        s = svc()
        since = s.clock() - timedelta(days=days)
        series = await asyncio.to_thread(s.store.history, window, since, metric)
        return _json({"window": window, "days": days, "metric": metric,
                      "metrics": sorted(series), "series": series})

    @app.get("/api/benchmark/sample")
    async def sample(n: int | None = Query(None, ge=1), window: str = "24h",
                     seed: int | None = None) -> Response:
        h = hours(window)
        s = svc()
        n = min(n or s.cfg.sample_default_n, s.cfg.sample_max_n)
        now = s.clock()
        body = await asyncio.to_thread(build_sample, s.src, s.store,
                                       start=now - timedelta(hours=h), now=now, n=n, seed=seed)
        body["window"] = window
        return _json(body)

    @app.post("/api/benchmark/labels")
    async def add_label(label: LabelIn) -> Response:
        s = svc()

        def exists() -> bool:
            with s.src.events_db() as db:
                return db is not None and db.execute(
                    "SELECT 1 FROM events WHERE id = ?", (label.event_id,)).fetchone() is not None

        if not await asyncio.to_thread(exists):
            raise HTTPException(404, "unknown event")
        row = await asyncio.to_thread(s.store.save_label, label.event_id, label.correct_event,
                                      label.correct_location, label.notes)
        summary = await asyncio.to_thread(s.labels_summary)
        return _json({"label": row, "summary": summary})

    @app.get("/api/benchmark/labels")
    async def labels() -> Response:
        s = svc()
        items = await asyncio.to_thread(s.store.labels)
        summary = await asyncio.to_thread(s.labels_summary)
        return _json({"count": len(items), "items": list(items.values()), "summary": summary})
