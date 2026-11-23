"""Storylines API: threads of linked events derived by the worker (osint.storylines).

GET /api/storylines[?bbox=w,s,e,n&since=&until=&hours=&category=&theatre=&q=&min_events=
    &limit=]                              -> {items, theatres, job}; latest activity first
GET /api/storylines/theatres[?hours=]     -> theatres (country x category family) one level up
GET /api/storylines/{id}                  -> thread: members (role, best-known time), links
                                             with reasons, places, bounds
GET /api/storylines/{id}/timeline         -> member timelines merged (storylines.timeline)
GET /api/events/{id}/storyline            -> {storyline: membership or null}

Reads only: linking runs in the worker. Times are best-known *event* times (occurred band
when stated and finer than a day, else the first report), each with its basis; never the
time the system processed something.
"""

import asyncio
from datetime import datetime, timedelta

import msgspec
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

from osint.api.events import parse_bbox
from osint.api.history import parse_iso
from osint.core.geo import utcnow
from osint.storylines import timeline as story_timeline
from osint.storylines.store import get_store
from osint.timeline.build import build_timeline

_ENCODER = msgspec.json.Encoder()
SLIM = ("id", "title", "title_basis", "theatre", "theatre_label", "t_first", "t_first_basis",
        "t_last", "first_report", "last_report", "n_events", "n_news", "n_sensor",
        "categories", "places", "countries", "roles", "min_lat", "min_lon", "max_lat",
        "max_lon", "lat", "lon", "origin_event_id", "representative_event_id", "top_status",
        "updated_at")


def _json(obj: object) -> Response:
    return Response(_ENCODER.encode(obj), media_type="application/json")


def _when(raw: str | None, name: str) -> datetime | None:
    if not raw:
        return None
    try:
        return parse_iso(raw)
    except ValueError:
        raise HTTPException(422, f"invalid {name}") from None


def slim(s: dict) -> dict:
    return {k: s.get(k) for k in SLIM}


def storyline_timeline(runtime, story: dict) -> dict:
    """Merged timeline of a thread from the member events' own timelines."""
    from osint.api.intel import full_event
    from osint.imagery.job import get_store as imagery_store

    timelines: dict[str, dict] = {}
    images = imagery_store(runtime)
    for m in story.get("members") or []:
        ev = full_event(runtime, m["event_id"])
        if ev is None:  # merged away / deleted since the pass: the next pass catches up
            continue
        timelines[m["event_id"]] = build_timeline(
            ev, corroborations=ev.get("corroborations") or [],
            imagery=images.get_assets(ev["id"]),
            status_history=runtime.store.status_history(ev["id"]))
    return story_timeline.merge(story, timelines)


def mount_storylines(app: FastAPI, runtime) -> None:
    @app.get("/api/storylines")
    async def storylines(bbox: str | None = None, since: str | None = None,
                         until: str | None = None,
                         hours: float | None = Query(None, gt=0, le=24 * 365),
                         category: str | None = Query(None, max_length=200),
                         theatre: str | None = Query(None, max_length=80),
                         q: str | None = Query(None, max_length=120),
                         min_events: int = Query(2, ge=1, le=1000),
                         limit: int = Query(100, ge=1, le=500)) -> Response:
        box = parse_bbox(bbox)
        lo, hi = _when(since, "since"), _when(until, "until")
        if hours is not None:  # the last N hours of *event* time
            recent = utcnow() - timedelta(hours=hours)
            lo = max(lo, recent) if lo else recent

        def read() -> dict:
            st = get_store(runtime)
            items = st.list(bbox=box, since=lo, until=hi, category=category,
                            theatre=theatre, q=q, min_events=min_events, limit=limit)
            return {"items": [slim(s) for s in items], "theatres": st.theatres(since=lo),
                    "job": st.get_meta("status")}

        return _json(await asyncio.to_thread(read))

    @app.get("/api/storylines/theatres")
    async def theatres(hours: float | None = Query(None, gt=0, le=24 * 365)) -> Response:
        lo = utcnow() - timedelta(hours=hours) if hours else None
        return _json(await asyncio.to_thread(lambda: get_store(runtime).theatres(since=lo)))

    @app.get("/api/storylines/{storyline_id}")
    async def storyline(storyline_id: str) -> Response:
        s = await asyncio.to_thread(get_store(runtime).get, storyline_id)
        if s is None:
            raise HTTPException(404, "unknown storyline")
        return _json(s)

    @app.get("/api/storylines/{storyline_id}/timeline")
    async def storyline_tl(storyline_id: str) -> Response:
        def build() -> dict | None:
            s = get_store(runtime).get(storyline_id)
            return None if s is None else storyline_timeline(runtime, s)

        out = await asyncio.to_thread(build)
        if out is None:
            raise HTTPException(404, "unknown storyline")
        return _json(out)

    @app.get("/api/events/{event_id}/storyline")
    async def event_storyline(event_id: str) -> Response:
        def read() -> dict:
            eid = runtime.store.resolve_event_id(event_id)
            return {"event_id": eid,
                    "storyline": get_store(runtime).for_events([eid]).get(eid)}

        return _json(await asyncio.to_thread(read))
