"""Intel pane API: all-source aggregation around a selection, event timelines and
single-event export (GeoJSON / KML).

GET /api/intel?kind=event&id=...            (or kind=fire|quake|aircraft|ship|place|
    facility|point&lat=&lon=[&t=&id=&label=], or kind=actor&q=...)
    [&radius_km=&sections=assessment,timeline,...]  -> {subject, sections: {name: ...}}
GET /api/intel/pageviews?title=&project=en.wikipedia
                                             -> daily Wikipedia pageviews, 60 days (cached)
GET /api/events/{id}/timeline                -> osint.timeline.build.build_timeline
GET /api/events/{id}/export?format=geojson|kml
"""

import asyncio

import httpx
import msgspec
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

from osint.api import kml
from osint.api.events import CATEGORY_COLORS, DEFAULT_COLOR, add_corroboration, impact_detail
from osint.intel import aggregate, context, hooks
from osint.translate import english
from osint.translate import job as translate_job

_ENCODER = msgspec.json.Encoder()
KINDS = ("event", "fire", "quake", "hazard", "aircraft", "ship", "place", "facility", "point",
         "actor")


def _json(obj: object) -> Response:
    return Response(_ENCODER.encode(obj), media_type="application/json")


def full_event(runtime, event_id: str) -> dict | None:
    """The event detail as GET /api/events/{id} builds it (documents translated,
    corroborations, verification, impact)."""
    store = runtime.store
    ev = store.get_event(event_id)
    if ev is None:
        return None
    add_corroboration(store, [ev])
    ev["corroborations"] = store.list_corroborations(event_id)
    tstore = translate_job.get_store(runtime)
    english.annotate_events(tstore, [ev], summaries=True,
                            extractions=getattr(runtime, "extraction_store", None))
    english.annotate_documents(tstore, ev.get("documents") or [])
    v = store.verifications([event_id]).get(event_id)
    ev["verification"] = None if v is None else {
        "verdict": v["verdict"], "reasons": v["reasons"], "quotes": v["quotes"]}
    ev["impact"] = impact_detail(store.impacts([event_id]).get(event_id))
    return ev


def event_geojson(ev: dict) -> dict:
    """FeatureCollection: the event point, its impact sites / areas and sensor evidence."""
    props = {k: ev.get(k) for k in ("id", "title", "title_en", "category", "status",
                                    "confidence", "t_start", "t_end", "place_name", "country",
                                    "precision", "summary_en", "summary")}
    props["sources"] = [{"source": d.get("source"), "url": d.get("url"),
                         "published_at": d.get("published_at")}
                        for d in ev.get("documents") or []]
    feats = []
    if ev.get("lat") is not None:
        feats.append({"type": "Feature", "properties": {**props, "role": "event"},
                      "geometry": {"type": "Point", "coordinates": [ev["lon"], ev["lat"]]}})
    imp = ev.get("impact") or {}
    for s in imp.get("sites") or []:
        if s.get("lat") is not None:
            feats.append({"type": "Feature", "properties": {
                "role": "impact_site", "name": s.get("name"), "precision": s.get("precision")},
                "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]}})
    for a in imp.get("areas") or []:
        if a.get("geometry"):
            feats.append({"type": "Feature", "properties": {
                "role": "impact_area", "kind": a.get("kind"), "basis": a.get("basis")},
                "geometry": a["geometry"]})
    for c in ev.get("corroborations") or []:
        d = c.get("details") or {}
        if d.get("lat") is not None:
            feats.append({"type": "Feature", "properties": {
                "role": "evidence", "kind": c.get("kind"), "ref_id": c.get("ref_id"),
                "rule": c.get("rule"), "score": c.get("score"), "ts": d.get("ts"),
                "distance_km": c.get("distance_km")},
                "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]}})
    return {"type": "FeatureCollection", "features": feats}


def event_kml(ev: dict) -> str:
    color = CATEGORY_COLORS.get(ev.get("category") or "", DEFAULT_COLOR)
    title = ev.get("title_en") or ev.get("title") or ev.get("id") or "event"
    pms = []
    if ev.get("lat") is not None:
        rows = [("Category", ev.get("category")), ("Status", ev.get("status")),
                ("Confidence", f"{float(ev.get('confidence') or 0):.2f}"),
                ("Place", ev.get("place_name")), ("Precision", ev.get("precision")),
                ("First report", ev.get("t_start")), ("Latest report", ev.get("t_end")),
                ("Summary", ev.get("summary_en") or ev.get("summary")),
                *[(d.get("source"), d.get("url")) for d in ev.get("documents") or []],
                ("Event id", ev.get("id"))]
        pms.append(kml._placemark(title, kml._table(rows),
                                  kml._style(kml.ICON + "placemark_circle.png", color, 1.4),
                                  float(ev["lon"]), float(ev["lat"]), when=ev.get("t_start")))
    for c in ev.get("corroborations") or []:
        d = c.get("details") or {}
        if d.get("lat") is None:
            continue
        rows = [("Kind", c.get("kind")), ("Rule", c.get("rule")), ("Time", d.get("ts")),
                ("Distance km", c.get("distance_km")), ("Score", c.get("score")),
                ("FRP MW", d.get("frp_mw")), ("Satellite", d.get("satellite"))]
        pms.append(kml._placemark(f"{c.get('kind')} {c.get('ref_id')}", kml._table(rows),
                                  kml._style(kml.ICON + "firedept.png", "ff1a6aff", 0.8),
                                  float(d["lon"]), float(d["lat"]), when=d.get("ts")))
    return kml._doc(title, pms)


def mount_intel(app: FastAPI, runtime) -> None:
    if context.watch_flags not in hooks.TRACK_FLAGGERS:
        hooks.TRACK_FLAGGERS.append(context.watch_flags)

    @app.get("/api/intel")
    async def intel(kind: str = Query(..., pattern="^(" + "|".join(KINDS) + ")$"),
                    id: str | None = Query(None, max_length=200),
                    lat: float | None = Query(None, ge=-90, le=90),
                    lon: float | None = Query(None, ge=-180, le=180),
                    t: str | None = Query(None, max_length=40),
                    q: str | None = Query(None, max_length=120),
                    label: str | None = Query(None, max_length=200),
                    radius_km: float | None = Query(None, gt=0, le=aggregate.MAX_RADIUS_KM),
                    sections: str | None = None) -> Response:
        want = [s for s in (sections or ",".join(aggregate.SECTIONS)).split(",")
                if s in aggregate.SECTIONS]
        now = aggregate.utcnow()

        def resolve() -> dict:
            subj = aggregate.resolve_subject(runtime, kind=kind, id=id, lat=lat, lon=lon, t=t,
                                             q=q, label=label, radius_km=radius_km)
            if kind == "event":
                subj["event"] = full_event(runtime, id or "")
                subj["label"] = (subj["event"].get("title_en") or subj["event"].get("title"))
            if kind != "actor":
                subj["cc"] = context.resolve_cc(runtime, subj)
            return subj

        try:
            subj = await asyncio.to_thread(resolve)
        except aggregate.SubjectError as exc:
            raise HTTPException(404 if kind == "event" else 422, str(exc)) from None
        # assessment reads the imagery and timeline results: compute them when needed
        need = set(want) | ({"imagery", "timeline"} if "assessment" in want else set())
        simple = [s for s in need if s in aggregate.SIMPLE]
        res = await asyncio.gather(*(asyncio.to_thread(aggregate.run_section, s, runtime, subj,
                                                       now) for s in simple))
        ctx = dict(zip(simple, res, strict=True))
        if "timeline" in need:
            ctx["timeline"] = await asyncio.to_thread(aggregate.run_section, "timeline",
                                                      runtime, subj, now, ctx)
        if "assessment" in need:
            ctx["assessment"] = await asyncio.to_thread(aggregate.run_section, "assessment",
                                                        runtime, subj, now, ctx)
        ev = subj.pop("event", None)
        subject = {k: v for k, v in subj.items() if k != "track"}
        if ev:
            subject.update({k: ev.get(k) for k in ("category", "status", "confidence",
                                                   "place_name", "country", "precision",
                                                   "t_start", "t_end")})
        if subj.get("track"):
            subject["track"] = {k: subj["track"].get(k) for k in (
                "name", "ident", "reg", "type_code", "operator", "is_mil", "high_priority")}
        return _json({"subject": subject, "generated_at": now.isoformat(timespec="seconds"),
                      "sections": {s: ctx.get(s) for s in want}})

    @app.get("/api/intel/pageviews")
    async def intel_pageviews(title: str = Query(..., min_length=1, max_length=250),
                              project: str = Query("en.wikipedia",
                                                   pattern=r"^[a-z\-]{2,12}\.wikipedia$")
                              ) -> Response:
        try:
            out = await asyncio.to_thread(context.pageviews, title, project)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Wikimedia pageviews: {type(exc).__name__}") from None
        return _json(out)

    @app.get("/api/events/{event_id}/timeline")
    async def event_timeline(event_id: str) -> Response:
        def build() -> dict | None:
            ev = full_event(runtime, event_id)
            if ev is None:
                return None
            subj = {"kind": "event", "id": event_id, "event": ev}
            return aggregate.section_timeline(runtime, subj, aggregate.utcnow(), {})

        out = await asyncio.to_thread(build)
        if out is None:
            raise HTTPException(404, "unknown event")
        return _json(out)

    @app.get("/api/events/{event_id}/export")
    async def event_export(event_id: str,
                           format: str = Query("geojson", pattern="^(geojson|kml)$")
                           ) -> Response:
        ev = await asyncio.to_thread(full_event, runtime, event_id)
        if ev is None:
            raise HTTPException(404, "unknown event")
        name = f"event-{event_id}"
        if format == "kml":
            body, media, ext = event_kml(ev), kml.KML_MEDIA, "kml"
        else:
            body, media, ext = _ENCODER.encode(event_geojson(ev)), "application/geo+json", \
                "geojson"
        return Response(body, media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{name}.{ext}"'})
