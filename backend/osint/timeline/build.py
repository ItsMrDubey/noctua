"""Per-event timeline: one chronologically ordered list of what is known about *when*.

Three kinds of time are kept apart and labelled:

  occurred  when it happened, as claimed by a source ("when" extraction, resolved by
            osint.timeline.when into a band with an honest precision label)
  reported  when each source published (documents' published_at)
  observed  when a sensor saw something (FIRMS scan time, quake origin time, hazard alert,
            aircraft / ship closest approach) or a satellite image was acquired

plus ``milestone`` rows: first report, first sensor detection, first post-event imagery,
recorded status changes and when the system first ingested the event.

The builder is pure (dicts in, dict out) so a storyline view can merge several event
timelines. Entries are sorted by their best-known time (``t`` or band start); claims with
no usable time are returned under ``unplaced``. Claims from different documents whose
bands do not overlap are flagged ``conflict`` and listed in ``occurred.conflicts`` side by
side, never silently resolved.
"""

import math
from datetime import UTC, datetime

from osint.core.geo import haversine_km
from osint.timeline.when import local_offset_h, parse_when

OBSERVED_LABEL = {"fire": "FIRMS thermal hotspot", "quake": "Seismic event",
                  "hazard": "Hazard alert", "aircraft": "Military aircraft nearby",
                  "ship": "Vessel nearby", "ais_gap": "AIS gap", "imagery": "Imagery"}


def parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat(timespec="seconds")


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def compass(deg: float) -> str:
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[int((deg + 22.5) // 45) % 8]


def _doc_label(doc: dict) -> str:
    return str(doc.get("source") or "source")


def _occurred(event: dict, docs: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Occurred claims from the extracted ``when`` fields (one per distinct phrase+doc)."""
    placed, unplaced = [], []
    seen: dict[tuple, dict] = {}
    for f in event.get("fields") or []:
        if f.get("field") != "when" or not f.get("value"):
            continue
        doc = docs.get(f.get("doc_id")) or {}
        pub = parse_ts(doc.get("published_at")) or parse_ts(event.get("t_start"))
        band = parse_when(f["value"], pub, event.get("lon")) if pub else None
        base = {"kind": "occurred", "phrase": f["value"], "quote": f.get("quote") or None,
                "doc_id": f.get("doc_id"), "source": _doc_label(doc) if doc else None,
                "url": doc.get("url"), "flags": []}
        if band is None:
            unplaced.append({**base, "label": "Stated time (not resolvable to a date)",
                             "precision": "unparsed", "t": None, "lo": None, "hi": None})
            continue
        key = (band.lo, band.hi, band.precision)
        if key in seen:  # same band from another phrase / doc: one row, more sources
            seen[key]["also"].append({"source": base["source"], "doc_id": base["doc_id"],
                                      "phrase": base["phrase"]})
            continue
        entry = {**base, **band.as_dict(), "t": None, "also": [],
                 "label": "Scheduled / announced" if band.future else "Occurred (claimed)"}
        if band.future:
            entry["flags"].append("future")
        seen[key] = entry
        placed.append(entry)
    return placed, unplaced


def _conflicts(claims: list[dict]) -> list[list[int]]:
    """Pairs of claim indexes (into ``claims``) from different documents whose bands do
    not overlap."""
    out = []
    for i, a in enumerate(claims):
        for j in range(i + 1, len(claims)):
            b = claims[j]
            if a.get("doc_id") == b.get("doc_id") or a.get("future") or b.get("future"):
                continue
            if a["hi"] <= b["lo"] or b["hi"] <= a["lo"]:
                out.append([i, j])
    return out


def _fire_groups(event: dict, rows: list[dict]) -> list[dict]:
    """FIRMS hotspots from one satellite scan (same ts + satellite) collapse to one row."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        d = r.get("details") or {}
        groups.setdefault((d.get("ts"), d.get("satellite")), []).append(r)
    out = []
    for (ts, sat), g in groups.items():
        frps = [float((r.get("details") or {}).get("frp_mw") or 0) for r in g]
        dists = [r.get("distance_km") for r in g if r.get("distance_km") is not None]
        best = max(g, key=lambda r: r.get("score") or 0)
        bd = best.get("details") or {}
        n = len(g)
        detail = (f"{n} hotspot{'s' if n != 1 else ''} · {sat or 'VIIRS/MODIS'} scan · "
                  f"max FRP {max(frps):.0f} MW")
        if dists:
            detail += f" · {min(dists):.1f}–{max(dists):.1f} km" if n > 1 else \
                f" · {dists[0]:.1f} km"
        if bd.get("lat") is not None and event.get("lat") is not None:
            detail += " " + compass(bearing_deg(event["lat"], event["lon"], bd["lat"], bd["lon"]))
        out.append({"kind": "observed", "sensor": "fire", "t": ts, "precision": "scan",
                    "label": OBSERVED_LABEL["fire"], "detail": detail, "count": n,
                    "ref_ids": [r["ref_id"] for r in g], "lat": bd.get("lat"),
                    "lon": bd.get("lon"), "flags": [], "linked_at": min(
                        (r.get("created_at") for r in g if r.get("created_at")), default=None)})
    return out


def _observed(event: dict, corroborations: list[dict]) -> list[dict]:
    fires = [c for c in corroborations if c.get("kind") == "fire"]
    out = _fire_groups(event, fires)
    for c in corroborations:
        if c.get("kind") == "fire":
            continue
        d = c.get("details") or {}
        kind = c.get("kind")
        bits = []
        if kind == "quake":
            bits = [f"M{d['magnitude']:.1f}" if d.get("magnitude") is not None else None,
                    d.get("event_type"), d.get("place")]
        elif kind == "hazard":
            bits = [d.get("source"), d.get("hazard"), d.get("alert_level"), d.get("name")]
        elif kind in ("aircraft", "ship"):
            bits = [d.get("name") or d.get("callsign") or c.get("ref_id"), d.get("type_code")]
        if c.get("distance_km") is not None:
            bits.append(f"{c['distance_km']:.1f} km")
        out.append({"kind": "observed", "sensor": kind, "t": d.get("ts"),
                    "precision": "origin" if kind == "quake" else "exact",
                    "label": OBSERVED_LABEL.get(kind, kind), "url": d.get("url"),
                    "detail": " · ".join(str(b) for b in bits if b), "count": 1,
                    "ref_ids": [c.get("ref_id")], "lat": d.get("lat"), "lon": d.get("lon"),
                    "flags": [], "linked_at": c.get("created_at")})
    return out


def _imagery(assets: list[dict]) -> list[dict]:
    out = []
    for a in assets:
        if a.get("status") != "ok" or not a.get("acquired_at"):
            continue
        before = a.get("role") == "before"
        cloud = a.get("cloud_pct")
        out.append({"kind": "observed", "sensor": "imagery", "t": a["acquired_at"],
                    "precision": "pass",
                    "label": "Baseline imagery (pre-event)" if before else "Post-event imagery",
                    "detail": " · ".join(x for x in (
                        a.get("collection"), a.get("scene_id"),
                        f"cloud {cloud:.0f}%" if cloud is not None else None) if x),
                    "role": a.get("role"), "flags": []})
    return out


def _status(history: list[dict]) -> list[dict]:
    out = []
    for h in history:
        if h.get("source") == "baseline":
            label = f"Status {h['status']} (as of history start; earlier changes not recorded)"
        elif h.get("prev_status"):
            label = f"Status {h['prev_status']} → {h['status']}"
        else:
            label = f"Status {h['status']} (first recorded)"
        out.append({"kind": "milestone", "milestone": "status", "t": h.get("at"),
                    "precision": "recorded", "label": label, "status": h.get("status"),
                    "prev_status": h.get("prev_status"), "baseline": h.get("source") == "baseline",
                    "flags": []})
    return out


def sort_key(e: dict) -> tuple:
    t = parse_ts(e.get("t") or e.get("lo"))
    rank = {"occurred": 0, "observed": 1, "reported": 2, "milestone": 3}.get(e["kind"], 4)
    return (t or datetime.max.replace(tzinfo=UTC), rank)


def build_timeline(event: dict, *, corroborations: list[dict] | None = None,
                   imagery: list[dict] | None = None, status_history: list[dict] | None = None
                   ) -> dict:
    """Timeline dict for one event (``event`` = full record with documents and fields)."""
    docs_list = sorted(event.get("documents") or [], key=lambda d: d.get("published_at") or "")
    docs = {d.get("doc_id"): d for d in docs_list}
    entries: list[dict] = []

    # reported: one row per document (reposts kept: each is a real publication)
    for d in docs_list:
        entries.append({"kind": "reported", "t": d.get("published_at"), "precision": "exact",
                        "label": "Reported", "source": _doc_label(d), "doc_id": d.get("doc_id"),
                        "url": d.get("url"), "lang": d.get("lang") or d.get("language"),
                        "title": d.get("title_en") or d.get("title"), "role": d.get("role"),
                        "flags": []})
    claims, unplaced = _occurred(event, docs)
    entries += claims
    observed = _observed(event, corroborations or [])
    entries += observed
    imagery_rows = _imagery(imagery or [])
    entries += imagery_rows
    entries += _status(status_history or [])
    if event.get("first_seen"):
        entries.append({"kind": "milestone", "milestone": "ingested", "t": event["first_seen"],
                        "precision": "recorded", "label": "First ingested by this system",
                        "flags": []})
    linked = [o["linked_at"] for o in observed if o.get("linked_at")]
    if linked:
        entries.append({"kind": "milestone", "milestone": "sensor_linked", "t": min(linked),
                        "precision": "recorded", "label": "Sensor evidence first linked",
                        "flags": []})

    # flags: first report, first sensor detection, first post-event imagery
    reported = [e for e in entries if e["kind"] == "reported" and parse_ts(e["t"])]
    first_report = min(reported, key=lambda e: parse_ts(e["t"])) if reported else None
    if first_report:
        first_report["flags"].append("first_report")
    sensed = [e for e in observed if parse_ts(e.get("t"))]
    if sensed:
        min(sensed, key=lambda e: parse_ts(e["t"]))["flags"].append("first_sensor")
    post = [e for e in imagery_rows if e.get("role") == "after"]
    if post:
        min(post, key=lambda e: parse_ts(e["t"]))["flags"].append("first_imagery")

    # conflicts between occurred claims of different documents
    past = [c for c in claims if not c.get("future")]
    pairs = _conflicts(past)
    for i, j in pairs:
        past[i]["flags"].append("conflict")
        past[j]["flags"].append("conflict")
    # best occurred window: the intersection of all (non-future) claims when they agree,
    # else none (the conflict is shown instead); with no claim at all, only an upper bound
    occurred: dict = {"lo": None, "hi": None, "precision": None, "basis": "none",
                      "conflicts": [[past[i]["doc_id"], past[j]["doc_id"]] for i, j in pairs]}
    if past and not pairs:
        lo = max(parse_ts(c["lo"]) for c in past)
        hi = min(parse_ts(c["hi"]) for c in past)
        if lo < hi:
            finest = min(past, key=lambda c: parse_ts(c["hi"]) - parse_ts(c["lo"]))
            occurred.update(lo=iso(lo), hi=iso(hi), precision=finest["precision"],
                            basis="claims", qualifier=finest.get("qualifier"),
                            tz_basis=finest.get("tz_basis"), n_claims=len(past))
    if occurred["basis"] == "none" and first_report:
        occurred.update(hi=first_report["t"], basis="before_first_report",
                        precision="upper_bound")

    t0 = parse_ts(first_report["t"]) if first_report else None
    for e in entries:
        t = parse_ts(e.get("t") or e.get("lo"))
        e["t_rel_min"] = None if (t is None or t0 is None) else round(
            (t - t0).total_seconds() / 60.0, 1)
    entries.sort(key=sort_key)
    return {
        "event_id": event.get("id"),
        "title": event.get("title_en") or event.get("title"),
        "anchor": {"first_report": first_report["t"] if first_report else None},
        "local_offset_h": local_offset_h(event.get("lon")),
        "occurred": occurred,
        "entries": entries,
        "unplaced": unplaced,
        "status_history_recorded": any(h.get("source") != "baseline"
                                       for h in status_history or []),
    }


def distance_km(event: dict, lat: float | None, lon: float | None) -> float | None:
    if lat is None or lon is None or event.get("lat") is None:
        return None
    return haversine_km(event["lat"], event["lon"], lat, lon)


__all__ = ["bearing_deg", "build_timeline", "compass", "distance_km", "parse_ts"]
