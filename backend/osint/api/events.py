"""Events API: map list, detail, stats (GDELT scorecard v1) and KML for Google Earth Pro."""

import asyncio
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import escape

import msgspec
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response

from osint.api import kml
from osint.core import countries
from osint.core.sensor_status import SIGNAL_DETECTORS, detection_kinds, sensor_label
from osint.enrich.rules import RULE_FIELDS
from osint.store.country_sql import INVOLVEMENTS, LOCATIONS
from osint.translate import english
from osint.translate import job as translate_job

MAX_LIMIT = 10000
COUNTRY_WINDOW = timedelta(days=7)  # default time window of the country view
MAX_COUNTRY_WINDOW = timedelta(days=365)  # the widest window a country query may span
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


def country_code(raw: str | None) -> str | None:
    """A known ISO 3166-1 alpha-2 code in any case -> upper-case ISO2; anything else -> None."""
    if not raw or not raw.strip():
        return None
    return countries.to_iso2(raw.strip().upper(), hint="iso2")


def unknown_country() -> JSONResponse:
    return JSONResponse({"error": "unknown_country"}, status_code=400)


def _bad(error: str) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=400)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def clamp_since(since: datetime, until: datetime) -> datetime:
    """``since`` no earlier than ``until`` minus MAX_COUNTRY_WINDOW (bounds country queries)."""
    return max(since, until - MAX_COUNTRY_WINDOW)


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


def sensor_block(row: dict | None) -> dict | None:
    """Detector, sensor label and stored detection details (lifecycle, readings) of a
    sensor-derived event; evidence from its own detector is detection, not corroboration."""
    if row is None:
        return None
    det = row["detector"]
    return {"detector": det, "sensor": sensor_label(det), "signal": det in SIGNAL_DETECTORS,
            "detection_kinds": sorted(detection_kinds(det)), "cell": row.get("cell"),
            "details": row.get("details") or {}}


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


def _count(value: object) -> int | None:
    """A casualty count as a non-negative int ("12" -> 12, 12.0 -> 12); anything else (None,
    bool, "many", "12a", -1) -> None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        n = int(value) if isinstance(value, int | float) else int(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return None
    return n if n >= 0 else None


def field_precedence(rule_rows: list[dict], extraction: dict | None) -> dict:
    """Extracted fields of an event (deterministic extraction spec §2.4): a value of an LLM
    extraction whose verdict is ``supported`` outranks the rule value
    (``event_rule_fields``); otherwise the rule value is used. ``field_source`` says which one
    each field came from (None: neither has a value). Both stay stored."""
    rule: dict[str, list[dict]] = {}
    for r in rule_rows:
        rule.setdefault(r["field"], []).append(r)
    rec = extraction or {}
    llm: dict[str, object] = {}
    if rec.get("status") == "done" and rec.get("verdict") == "supported":
        res = rec.get("result") or {}
        cas = res.get("casualties") or {}
        llm = {"summary": str(res.get("summary") or "").strip() or None,
               "casualties.killed": _count(cas.get("killed")),
               "casualties.injured": _count(cas.get("injured")),
               "weapons_platforms": sorted({str(w) for w in res.get("weapons_platforms") or []
                                            if w}) or None}
    values: dict[str, object] = {}
    source: dict[str, str | None] = {}
    for f in RULE_FIELDS:
        if llm.get(f) is not None:
            values[f], source[f] = llm[f], "llm"
            continue
        rows = rule.get(f)
        if not rows:
            values[f] = [] if f == "weapons_platforms" else None
            source[f] = None
        elif f == "weapons_platforms":
            values[f], source[f] = sorted({r["value"] for r in rows}), "rule"
        elif f.startswith("casualties."):
            n = _count(rows[0]["value"])  # a malformed stored value is no value, not a 500
            values[f], source[f] = n, "rule" if n is not None else None
        else:
            values[f], source[f] = rows[0]["value"], "rule"
    return {"extracted": {"headline": values["headline"], "summary": values["summary"],
                          "casualties": {"killed": values["casualties.killed"],
                                         "injured": values["casualties.injured"]},
                          "weapons_platforms": values["weapons_platforms"]},
            "field_source": source}


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
        until: str | None = None,
        offset: int = Query(0, ge=0),
        country: str | None = None,
        location: str = "all",
        involvement: str = "all",
        counterpart: str | None = None,
        ids: bool = False,
    ) -> Response:
        """Map view by default: multi-source / sensor-corroborated / official events, or
        reported ones with city/point precision and confidence >= 0.5; located only.
        ``all=true`` returns everything; ``include_unlocated=true`` adds unlocated events.

        ``country`` (ISO2) is the country context view (spec B 2.1): ``location``,
        ``involvement`` and ``counterpart`` narrow it, every item carries ``ctx``
        ({location, involvement}), and items are ordered by lifecycle, then last update.
        Errors (HTTP 400): unknown country/counterpart -> {"error": "unknown_country"};
        bad location/involvement -> {"error": "bad_location"} / {"error": "bad_involvement"};
        counterpart == country -> {"error": "bad_counterpart"}. The window is always bounded:
        ``since`` defaults to now - 7 days and ``until`` to now, and the envelope then also reports the effective
        ``since``/``until`` (ISO) so a client paging with ``offset`` can pass the first page's
        ``until`` back and get a stable window. ``ids=true`` returns only the matching ids
        (the globe's dimming set; most recently updated first, rows are not decoded) plus ``truncated`` (true when as many ids as ``limit`` came
        back, so more may exist). ``since`` is clamped to at most 365 days before ``until``. Without ``country`` the response is unchanged."""
        since_dt, until_dt = parse_since(since), parse_since(until)
        cc = cp = None
        if country:
            cc = country_code(country)
            if cc is None:
                return unknown_country()
            if counterpart:
                cp = country_code(counterpart)
                if cp is None:
                    return unknown_country()
                if cp == cc:
                    return _bad("bad_counterpart")
            if location not in LOCATIONS:
                return _bad("bad_location")
            if involvement not in INVOLVEMENTS:
                return _bad("bad_involvement")
            now = _utcnow()
            if until_dt is None:
                until_dt = now  # pins the window, so offset paging is stable
            if since_dt is None:
                since_dt = now - COUNTRY_WINDOW
            since_dt = clamp_since(since_dt, until_dt)
        filters = {"bbox": parse_bbox(bbox), "since": since_dt, "until": until_dt,
                   "category": category, "status": status, "min_confidence": min_confidence,
                   "include_unlocated": all_ or include_unlocated,
                   "map_default": not all_ and not status, "country": cc,
                   "location": location, "involvement": involvement, "counterpart": cp}
        if ids:  # ids only: no row decoding, ctx, annotations or lifecycle sort
            got = await asyncio.to_thread(store.list_event_ids, limit=limit, offset=offset,
                                          **filters)
            return _json({"layer": "events", "count": len(got), "ids": got,
                          "truncated": len(got) >= limit})
        items = await asyncio.to_thread(store.list_events, limit=limit, offset=offset, **filters)
        await asyncio.to_thread(add_corroboration, store, items)
        await asyncio.to_thread(add_impacts, store, items)
        await asyncio.to_thread(add_english, items)
        out: dict = {"layer": "events", "count": len(items), "items": items}
        if cc:
            out["since"] = since_dt.isoformat() if since_dt else None
            out["until"] = until_dt.isoformat() if until_dt else None
        return _json(out)

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
        event_id = ev["id"]  # a merged-away id resolves to its survivor (redirected_from)
        await asyncio.to_thread(add_corroboration, store, [ev])
        ev["corroborations"] = await asyncio.to_thread(store.list_corroborations, event_id)
        if ev.get("origin") == "sensor":
            ev["sensor"] = sensor_block(await asyncio.to_thread(store.get_sensor_event, event_id))
        await asyncio.to_thread(add_english, [ev], summaries=True)
        await asyncio.to_thread(english.annotate_documents, translate_job.get_store(runtime),
                                ev.get("documents") or [])
        v = (await asyncio.to_thread(store.verifications, [event_id])).get(event_id)
        ev["verification"] = None if v is None else {
            "verdict": v["verdict"], "reasons": v["reasons"], "quotes": v["quotes"]}
        rec = (await asyncio.to_thread(store.impacts, [event_id])).get(event_id)
        ev["impact"] = impact_detail(rec)
        ev["impact_precision"] = ((rec or {}).get("best") or {}).get("precision")
        rule_rows = (await asyncio.to_thread(store.rule_fields, [event_id])).get(event_id, [])
        ext_store = getattr(runtime, "extraction_store", None)
        ext_rec = (await asyncio.to_thread(ext_store.get, event_id)
                   if ext_store is not None else None)
        ev.update(field_precedence(rule_rows, ext_rec))
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
