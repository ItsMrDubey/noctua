"""Events API: map list, detail, stats (GDELT scorecard v1) and KML for Google Earth Pro."""

import asyncio
from datetime import UTC, datetime
from xml.sax.saxutils import escape

import msgspec
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

from osint.api import kml
from osint.translate import english
from osint.translate import job as translate_job

MAX_LIMIT = 10000
IMPACT_LIMIT = 3000
KML_LIMIT = 5000
_ENCODER = msgspec.json.Encoder()

# category -> KML colour (aabbggrr)
CATEGORY_COLORS = {
    "armed_conflict": "ff0000ff",   # red
    "air_activity": "ff00a5ff",     # orange
    "maritime_security": "ffffbf00",  # deep sky blue
    "civil_unrest": "ff00ffff",     # yellow
    "disaster": "ff8b008b",         # purple
    "infrastructure": "ff13458b",   # brown
    "political": "ffffffff",        # white
    "economic": "ff00ff00",         # green
    "cyber_outage": "ffff00ff",     # magenta
}
DEFAULT_COLOR = "ffc0c0c0"


def parse_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    """``west,south,east,north`` in degrees; 422 when malformed."""
    if not raw:
        return None
    try:
        w, s, e, n = (float(x) for x in raw.split(","))
    except ValueError:
        raise HTTPException(422, "bbox must be west,south,east,north") from None
    if not (-180 <= w <= 180 and -180 <= e <= 180 and -90 <= s <= n <= 90):
        raise HTTPException(422, "bbox out of range")
    return w, s, e, n


def parse_since(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00").replace(" ", "+"))
    except ValueError:
        raise HTTPException(422, "since must be ISO-8601") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _json(obj: object) -> Response:
    return Response(_ENCODER.encode(obj), media_type="application/json")


def events_kml(items: list[dict]) -> str:
    pms = []
    for it in items[:KML_LIMIT]:
        if it.get("lat") is None or it.get("lon") is None:
            continue
        conf = float(it.get("confidence") or 0.0)
        color = CATEGORY_COLORS.get(it.get("category") or "", DEFAULT_COLOR)
        style = kml._style(kml.ICON + "placemark_circle.png", color, 0.6 + 1.2 * conf)
        rows = [("Category", it.get("category")), ("Status", it.get("status")),
                ("Confidence", f"{conf:.2f}"), ("Place", it.get("place_name")),
                ("Precision", it.get("precision")), ("Mentions", it.get("mention_count")),
                ("Outlets", it.get("source_diversity")), ("First report", it.get("t_start")),
                ("Latest report", it.get("t_end")),
                ("Summary", it.get("summary_en") or it.get("summary")),
                ("Event id", it.get("id"))]
        pms.append(kml._placemark(it.get("title_en") or it.get("title") or it.get("id") or "event",
                                  kml._table(rows), style, float(it["lon"]),
                                  float(it["lat"]), when=it.get("t_start")))
    return (f'{kml._NS}<Document><name>{escape("Events")}</name>{"".join(pms)}'
            "</Document></kml>")


def add_corroboration(store, items: list[dict]) -> None:
    """Add ``corroboration_score``, ``corroboration_kinds`` and ``source`` ('sensor' for
    sensor-derived events, else 'news') to event dicts in place."""
    ids = [it["id"] for it in items]
    summ = store.corroboration_summaries(ids)
    verdicts = store.verifications(ids)
    for it in items:
        it["verdict"] = (verdicts.get(it["id"]) or {}).get("verdict")
        s = summ.get(it["id"]) or {}
        it["corroboration_score"] = float(s.get("score") or 0.0)
        it["corroboration_kinds"] = list(s.get("kinds") or [])
        it["source"] = "sensor" if s.get("detector") else "news"
        if s.get("detector"):
            it["detector"] = s["detector"]


def add_impacts(store, items: list[dict]) -> None:
    """Slim impact fields on list items: ``impact_precision`` (best site precision label:
    exact / facility / neighbourhood / settlement / city, or None), ``impact_sites``
    (number of sites) and ``has_area``. The event's lat/lon already is the best site when
    that site is finer than the original location."""
    summ = store.impact_summaries([it["id"] for it in items])
    for it in items:
        s = summ.get(it["id"])
        it["impact_precision"] = s["impact_precision"] if s else None
        it["impact_sites"] = s["impact_sites"] if s else 0
        it["has_area"] = bool(s and s["has_area"])


def impact_detail(rec: dict | None) -> dict | None:
    """Full impact record for the event detail: sites (with evidence quotes, resolver and
    source), best site, affected areas (GeoJSON + basis) and the pre-impact location."""
    if rec is None:
        return None
    return {"sites": rec["sites"], "best": rec["best"], "areas": rec["areas"],
            "original_location": rec["origin"], "computed_at": rec["computed_at"],
            "version": rec["version"]}


def impact_layer_item(rec: dict) -> dict:
    """Compact layer row: located sites (no quotes) and area geometries with basis."""
    return {"event_id": rec["event_id"], "category": rec.get("category"),
            "status": rec.get("status"), "confidence": rec.get("confidence"),
            "t_start": rec.get("t_start"), "updated_at": rec.get("event_updated_at"),
            "sites": [{k: s.get(k) for k in ("name", "kind", "lat", "lon", "precision",
                                             "confidence", "resolver")}
                      for s in rec["sites"] if s.get("lat") is not None],
            "areas": [{k: a.get(k) for k in ("kind", "basis", "geometry", "radius_km")}
                      for a in rec["areas"]]}


def mount_events(app: FastAPI, runtime) -> None:
    store = runtime.store

    def add_english(items: list[dict], *, summaries: bool = False) -> None:
        """title_en / lang (+ summary_en) from the translation cache; None = not ready."""
        english.annotate_events(translate_job.get_store(runtime), items, summaries=summaries,
                                extractions=getattr(runtime, "extraction_store", None))

    @app.get("/api/events")
    async def list_events(
        bbox: str | None = None, since: str | None = None, category: str | None = None,
        status: str | None = None,
        min_confidence: float | None = Query(None, ge=0.0, le=1.0),
        limit: int = Query(1000, ge=1, le=MAX_LIMIT),
        all_: bool = Query(False, alias="all"),
        include_unlocated: bool = False,
    ) -> Response:
        """Map view by default: multi-source / sensor-corroborated / official events, or
        reported ones with city/point precision and confidence >= 0.5; located only.
        ``all=true`` returns everything; ``include_unlocated=true`` adds unlocated events."""
        items = await asyncio.to_thread(
            store.list_events, bbox=parse_bbox(bbox), since=parse_since(since),
            category=category, status=status, min_confidence=min_confidence, limit=limit,
            include_unlocated=all_ or include_unlocated,
            map_default=not all_ and not status)
        await asyncio.to_thread(add_corroboration, store, items)
        await asyncio.to_thread(add_impacts, store, items)
        await asyncio.to_thread(add_english, items)
        return _json({"layer": "events", "count": len(items), "items": items})

    @app.get("/api/events/impacts")
    async def impacts_layer(bbox: str | None = None, since: str | None = None,
                            limit: int = Query(1000, ge=1, le=IMPACT_LIMIT)) -> Response:
        """Impact sites + affected areas (GeoJSON, each with its ``basis``) of events
        whose sites/areas intersect ``bbox``; read-only (computed by the worker)."""
        recs = await asyncio.to_thread(store.impacts_in_bbox, parse_bbox(bbox),
                                       since=parse_since(since), limit=limit)
        items = [impact_layer_item(r) for r in recs]
        return _json({"layer": "impacts", "count": len(items), "items": items})

    @app.get("/api/events/stats")
    async def event_stats() -> Response:
        stats = await asyncio.to_thread(store.event_stats)
        stats["sensor"] = await asyncio.to_thread(store.corroboration_stats)
        job = getattr(runtime, "impact_job", None)
        if job is not None:
            stats["impact"] = job.status()
        corr = getattr(getattr(runtime, "event_builder", None), "corroborator", None)
        if corr is not None and corr.last is not None:
            stats["sensor"]["last_corroboration_pass"] = msgspec.to_builtins(
                corr.last.__dict__)
        det = getattr(runtime, "sensor_events", None)
        if det is not None:
            stats["sensor"]["detector"] = {"passes": det.passes, "last": det.last,
                                           "last_error": det.last_error}
        builder = getattr(runtime, "event_builder", None)
        if builder is not None:
            last = builder.last_pass
            stats["builder"] = {
                "passes": builder.passes, "last_error": builder.last_error,
                "last_pass": None if last is None else msgspec.to_builtins(last.__dict__),
            }
        return _json(stats)

    @app.get("/api/events/{event_id}")
    async def event_detail(event_id: str) -> Response:
        ev = await asyncio.to_thread(store.get_event, event_id)
        if ev is None:
            raise HTTPException(404, "unknown event")
        await asyncio.to_thread(add_corroboration, store, [ev])
        ev["corroborations"] = await asyncio.to_thread(store.list_corroborations, event_id)
        await asyncio.to_thread(add_english, [ev], summaries=True)
        await asyncio.to_thread(english.annotate_documents, translate_job.get_store(runtime),
                                ev.get("documents") or [])
        v = (await asyncio.to_thread(store.verifications, [event_id])).get(event_id)
        ev["verification"] = None if v is None else {
            "verdict": v["verdict"], "reasons": v["reasons"], "quotes": v["quotes"]}
        rec = (await asyncio.to_thread(store.impacts, [event_id])).get(event_id)
        ev["impact"] = impact_detail(rec)
        ev["impact_precision"] = ((rec or {}).get("best") or {}).get("precision")
        return _json(ev)

    @app.get("/kml/events.kml")
    async def events_kml_route(min_confidence: float | None = Query(None, ge=0.0, le=1.0)
                               ) -> Response:
        items = await asyncio.to_thread(store.list_events, min_confidence=min_confidence,
                                        limit=KML_LIMIT, slim=False)
        await asyncio.to_thread(add_english, items, summaries=True)
        return Response(await asyncio.to_thread(events_kml, items), media_type=kml.KML_MEDIA)

    @app.get("/api/translate/status")
    async def translate_status() -> Response:
        """Translation stage: calls today, queue, failures (cache stats without the job)."""
        job = getattr(runtime, "translate_job", None)
        if job is not None:
            return _json(await asyncio.to_thread(job.status))
        tstore = translate_job.get_store(runtime)
        if tstore is None:
            return _json({"enabled": False, "running": False})
        return _json(await asyncio.to_thread(translate_job.status, tstore,
                                             translate_job.load_config(runtime)))
