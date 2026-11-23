"""Intel-console section ``terror``: the Terror Watch view of the subject.

For an event that Terror Watch classified: the incident record (class, status with basis,
attribution box with its verbatim quote, casualties with every source, sources by language,
definition criteria). For any located subject: unique incidents within ``RADIUS_KM`` in the
last year (event time) and the country's posture summary. None when there is nothing to say.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from osint.core.geo import haversine_km
from osint.terror.store import get_store

RADIUS_KM = 300.0


def section_terror(runtime, subj: dict, now: datetime) -> dict | None:
    store = get_store(runtime)
    out: dict = {}
    ev = subj.get("event") or {}
    eid = ev.get("id") or (subj.get("id") if subj.get("kind") == "event" else None)
    if eid:
        rec = store.incident("ti-" + eid)
        if rec is not None and rec.get("merged_into"):
            rec = store.incident(rec["merged_into"]) or rec
        if rec is not None and rec.get("status") not in (None, "pending"):
            out["incident"] = rec
    lat, lon = subj.get("lat"), subj.get("lon")
    if lat is not None and lon is not None:
        since = (now - timedelta(days=365)).isoformat()
        near = []
        for i in store.incidents_near(lat, lon, RADIUS_KM / 111 + 0.5, since=since):
            if i["event_id"] == eid:
                continue
            d = haversine_km(lat, lon, i["lat"], i["lon"])
            if d <= RADIUS_KM:
                near.append({"id": i["id"], "event_id": i["event_id"], "title": i.get("title"),
                             "status": i["status"], "klass": i.get("klass"),
                             "sort_time": i.get("sort_time"), "distance_km": round(d),
                             "precision": i.get("precision"),
                             "group": (i.get("group") or {}).get("group_name")})
        near.sort(key=lambda x: x["distance_km"])
        out["nearby"] = {"radius_km": RADIUS_KM, "window_days": 365, "count": len(near),
                         "items": near[:12],
                         "basis": "real distances (haversine) from the subject; unique "
                                  "incidents by event time"}
    cc = (out.get("incident") or {}).get("country") or subj.get("cc") or ev.get("country")
    if cc:
        board = store.indicators(f"country:{cc}")
        if board:
            p = board.get("posture") or {}
            out["country"] = {"cc": cc, "name": board.get("name"),
                              "posture": {k: p.get(k) for k in ("available", "index", "band",
                                                                "coverage", "label")},
                              "official_levels": board.get("official_levels") or [],
                              "advisory": board.get("advisory"),
                              "computed_at": board.get("computed_at")}
    if not out.get("incident") and not (out.get("nearby") or {}).get("count") and \
            not out.get("country"):
        return None
    return out
