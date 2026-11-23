"""Intel aggregation: everything the platform's own stores know around one subject.

A *subject* is what the user selected: an event, a fire / quake observation, an aircraft
or ship track, a place or facility (a point), or an actor (a name, no point). It is
resolved to a centre point, a reference time and a search radius; each *section* then
reads one store and returns plain dicts:

  assessment  what is known / corroborated / unconfirmed (events: from status, verdict,
              corroboration, impact and imagery records; never free-text analysis)
  timeline    the event timeline (osint.timeline.build)
  related     events around the point (distance-ranked, 24 h / 7 d / 30 d counts, a
              30-day daily sparkline), same-place, same-category thread, same-actor events
  history     curated historical events (UCDP GED) near the point: totals by year / type
  sensors     FIRMS hotspots (with conflict score), quakes and hazard alerts near the point
  traffic     aircraft / ships near the point now, and (events) tracks that passed near it
              around the event time; TRACK_FLAGGERS add watchlist hits
  assets      reference assets (bases, nuclear sites, airports, ports, chokepoints, front
              line distance, webcams; ASSET_PROVIDERS add more) with distance and bearing
  imagery     before / after satellite imagery state (events)
  sources     the reports (outlet, language, time, link, repost groups) and diversity
  entities    actors and places to pivot on
  storyline   the thread an event belongs to (osint.storylines: role, span, members), or
              for a point / actor the storylines around it / naming it

Every section is independent (the API runs the requested ones concurrently) and returns
``{"error": ...}`` instead of raising, so one failing store never blanks the pane.
"""

import json
import logging
import math
import re
from datetime import UTC, datetime, timedelta

from osint.core.geo import haversine_km
from osint.intel import hooks
from osint.timeline.build import bearing_deg, build_timeline, compass, parse_ts

log = logging.getLogger(__name__)

SECTIONS = ("assessment", "timeline", "related", "history", "sensors", "traffic", "assets",
            "imagery", "sources", "entities", "storyline")
RADIUS_BY_PRECISION = {"point": 25.0, "city": 40.0, "admin1": 150.0, "country": 400.0}
MAX_RADIUS_KM = 500.0
DAY = timedelta(days=1)


# ------------------------------------------------------------------ helpers

def utcnow() -> datetime:
    return datetime.now(UTC)


def bbox_around(lat: float, lon: float, km: float) -> tuple[float, float, float, float]:
    dlat = km / 111.0
    dlon = km / (111.0 * max(0.05, math.cos(math.radians(lat))))
    w, e = lon - dlon, lon + dlon
    if w < -180:
        w += 360
    if e > 180:
        e -= 360
    return w, max(-90.0, lat - dlat), e, min(90.0, lat + dlat)


def geo(lat0: float, lon0: float, lat: float | None, lon: float | None) -> dict:
    """distance_km / bearing_deg / compass from the subject point."""
    if lat is None or lon is None:
        return {"distance_km": None, "bearing_deg": None, "compass": None}
    b = bearing_deg(lat0, lon0, lat, lon)
    return {"distance_km": round(haversine_km(lat0, lon0, lat, lon), 2),
            "bearing_deg": round(b), "compass": compass(b)}


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat(timespec="seconds")


def _attrs(raw: str | None) -> dict:
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def source_kind(source: str | None) -> str:
    """rss / telegram / gdelt / nga / ... from a document source name ("telegram:x")."""
    s = str(source or "")
    return s.split(":", 1)[0] if ":" in s else (s or "unknown")


_WS = re.compile(r"[\W_]+", re.UNICODE)


def _tokens(text: str | None) -> set[str]:
    return {t for t in _WS.split(str(text or "").casefold()) if len(t) > 2}


# ------------------------------------------------------------------ subject

class SubjectError(ValueError):
    pass


def resolve_subject(runtime, *, kind: str, id: str | None = None, lat: float | None = None,
                    lon: float | None = None, t: str | None = None, q: str | None = None,
                    label: str | None = None, radius_km: float | None = None) -> dict:
    """{kind, id, label, lat, lon, t, radius_km[, event]} for the selection."""
    now = utcnow()
    subj: dict = {"kind": kind, "id": id, "label": label, "lat": lat, "lon": lon,
                  "t": t, "q": q}
    if kind == "event":
        ev = runtime.store.get_event(id or "")
        if ev is None:
            raise SubjectError("unknown event")
        subj.update(lat=ev.get("lat"), lon=ev.get("lon"), t=ev.get("t_start"),
                    label=ev.get("title"), event=ev)
        radius_km = radius_km or RADIUS_BY_PRECISION.get(ev.get("precision") or "", 50.0)
    elif kind in ("aircraft", "ship"):
        p = runtime.ingestor.live.get(id) if id else None
        if p is not None:
            subj.update(lat=p.lat, lon=p.lon, t=p.ts.isoformat())
        track = runtime.store.get_tracks([id]).get(id) if id else None
        if track:
            subj["track"] = track
            subj["label"] = label or track.get("name") or track.get("ident") or id
    elif kind == "fire" and id and lat is None:
        from osint.fires.store import get_score_store
        row = get_score_store(runtime).get(id)
        if row:
            subj.update(lat=row["lat"], lon=row["lon"], t=row["ts"])
    elif kind == "actor":
        if not (q or label):
            raise SubjectError("actor needs q")
        subj["q"] = q or label
        subj["label"] = label or q
    if kind != "actor" and (subj["lat"] is None or subj["lon"] is None):
        raise SubjectError("subject has no location")
    subj["t"] = subj["t"] or _iso(now)
    subj["radius_km"] = float(min(MAX_RADIUS_KM, max(1.0, radius_km or 50.0)))
    return subj


# ------------------------------------------------------------------ sections

def section_related(runtime, subj: dict, now: datetime) -> dict:
    """Events near the point (or matching the actor): ranked, counted, sparkline."""
    store = runtime.store
    since = now - timedelta(days=30)
    self_id = subj.get("id") if subj["kind"] == "event" else None
    if subj["kind"] == "actor":
        items = store.search_events(subj["q"], since=since, limit=300)
        for it in items:
            it.pop("actors", None)
    else:
        lat, lon, r = subj["lat"], subj["lon"], subj["radius_km"] * 2
        items = store.list_events(bbox=bbox_around(lat, lon, r), since=since, limit=2000,
                                  include_unlocated=False, origin="news,sensor")
        for it in items:
            it.update(geo(lat, lon, it.get("lat"), it.get("lon")))
        items = [it for it in items if it["distance_km"] is not None and it["distance_km"] <= r]
    items = [it for it in items if it["id"] != self_id]
    counts = {"24h": 0, "7d": 0, "30d": 0}
    days = [0] * 30
    for it in items:
        t = parse_ts(it.get("t_end") or it.get("t_start"))
        if t is None:
            continue
        age = now - t
        counts["30d"] += age <= timedelta(days=30)
        counts["7d"] += age <= timedelta(days=7)
        counts["24h"] += age <= DAY
        idx = 29 - int(age / DAY)
        if 0 <= idx < 30:
            days[idx] += 1
    ev = subj.get("event") or {}
    place = (ev.get("place_name") or "").casefold()
    same_place = [it for it in items if place and (it.get("place_name") or "").casefold() == place]
    thread = []
    if ev:
        t0 = parse_ts(ev.get("t_start"))
        thread = [it for it in items if it.get("category") == ev.get("category") and t0
                  and parse_ts(it.get("t_start")) and abs(parse_ts(it["t_start"]) - t0)
                  <= timedelta(days=7)]
    actors_same: list[dict] = []
    names = [a.get("name") for a in (ev.get("actors") or []) if isinstance(a, dict)
             and a.get("name")]
    for name in names[:3]:
        for it in store.search_events(name, since=since, limit=40):
            if it["id"] != self_id and all(x["id"] != it["id"] for x in actors_same):
                it.pop("actors", None)
                it["actor"] = name
                if subj.get("lat") is not None:
                    it.update(geo(subj["lat"], subj["lon"], it.get("lat"), it.get("lon")))
                actors_same.append(it)
    key = (lambda it: it.get("distance_km") or 0.0) if subj["kind"] != "actor" else \
        (lambda it: -(parse_ts(it.get("t_end")) or now).timestamp())
    items.sort(key=key)
    slim = ("id", "title", "category", "status", "confidence", "t_start", "t_end", "lat",
            "lon", "place_name", "country", "distance_km", "bearing_deg", "compass",
            "mention_count", "source_diversity", "actor")
    pick = lambda xs, n: [{k: x.get(k) for k in slim if k in x} for x in xs[:n]]
    return {"radius_km": None if subj["kind"] == "actor" else subj["radius_km"] * 2,
            "counts": counts, "sparkline": {"days": days, "end": _iso(now)},
            "total": len(items), "nearest": pick(items, 25),
            "same_place": pick(same_place, 10), "thread": pick(thread, 10),
            "same_actors": pick(actors_same, 15)}


def section_history(runtime, subj: dict, now: datetime) -> dict:
    """Curated historical conflict events (UCDP GED) within the radius."""
    lat, lon, r = subj["lat"], subj["lon"], max(subj["radius_km"], 25.0)
    rows = runtime.store.list_events(bbox=bbox_around(lat, lon, r), status="historical",
                                     limit=5000, include_unlocated=False)
    rows = [x for x in rows if haversine_km(lat, lon, x["lat"], x["lon"]) <= r]
    fields = runtime.store.field_values([x["id"] for x in rows],
                                        ("fatalities_best", "type_of_violence", "dataset"))
    by_year: dict[str, dict] = {}
    by_type: dict[str, int] = {}
    deaths = 0
    for x in rows:
        f = fields.get(x["id"], {})
        y = str(x.get("t_start") or "")[:4]
        try:
            fat = int(float(f.get("fatalities_best") or 0))
        except ValueError:
            fat = 0
        deaths += fat
        yb = by_year.setdefault(y, {"events": 0, "fatalities": 0})
        yb["events"] += 1
        yb["fatalities"] += fat
        tv = f.get("type_of_violence") or x.get("subcategory") or "unspecified"
        by_type[tv] = by_type.get(tv, 0) + 1
    recent = sorted(rows, key=lambda x: x.get("t_start") or "", reverse=True)[:8]
    return {"radius_km": r, "total": len(rows), "fatalities_best": deaths,
            "by_year": dict(sorted(by_year.items())), "by_type": by_type,
            "datasets": sorted({fields.get(x["id"], {}).get("dataset") or "UCDP GED"
                                for x in rows}),
            "recent": [{"id": x["id"], "title": x.get("title"), "t_start": x.get("t_start"),
                        "lat": x["lat"], "lon": x["lon"],
                        **geo(lat, lon, x["lat"], x["lon"])} for x in recent]}


def _window(subj: dict, now: datetime, before_h: float, after_h: float) -> tuple:
    t = parse_ts(subj.get("t")) or now
    return t - timedelta(hours=before_h), min(now, t + timedelta(hours=after_h))


def _obs_near(runtime, typ: str, subj: dict, lo: datetime, hi: datetime, r: float,
              limit: int = 300) -> list[tuple]:
    w, s, e, n = bbox_around(subj["lat"], subj["lon"], r)
    lon_sql = "lon BETWEEN ? AND ?" if w <= e else "(lon >= ? OR lon <= ?)"
    sql = ("SELECT id, epoch_us(ts), lat, lon, value, attrs FROM observations WHERE type = ? "
           f"AND ts >= ? AND ts <= ? AND lat BETWEEN ? AND ? AND {lon_sql} "
           "QUALIFY row_number() OVER (PARTITION BY id ORDER BY version DESC) = 1 "
           f"ORDER BY ts DESC LIMIT {int(limit)}")
    return runtime.lake.query(sql, [typ, lo, hi, s, n, w, e], start=lo, end=hi)


def _us_iso(us: int) -> str:
    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=us)).isoformat()


def section_sensors(runtime, subj: dict, now: datetime) -> dict:
    """FIRMS / quake / hazard observations near the point around the reference time
    (events: 48 h before to 72 h after the start; otherwise the last 48 h)."""
    from osint.fires.store import get_score_store

    lat, lon, r = subj["lat"], subj["lon"], max(subj["radius_km"], 10.0)
    lo, hi = _window(subj, now, 48, 72) if subj["kind"] == "event" else (now - 2 * DAY, now)
    out: dict = {"window": {"from": _iso(lo), "to": _iso(hi)}, "radius_km": r}
    fires = []
    try:
        scores = get_score_store(runtime).between(lo, hi)
    except Exception:  # noqa: BLE001 — score store absent: hotspots without scores
        scores = {}
    for id_, us, la, lo_, val, attrs in _obs_near(runtime, "fire", subj, lo, hi, r):
        a = _attrs(attrs)
        sc = scores.get(id_)
        fires.append({"id": id_, "ts": _us_iso(us), "lat": la, "lon": lo_, "frp_mw": val,
                      "satellite": a.get("satellite"), "confidence": a.get("confidence"),
                      "daynight": a.get("daynight"),
                      "conflict_score": sc[0] if sc else None,
                      "conflict_reasons": [x for x in (sc[1] if sc else "").split(",") if x],
                      **geo(lat, lon, la, lo_)})
    fires.sort(key=lambda f: f["distance_km"])
    out["fires"] = {"count": len(fires), "items": fires[:60],
                    "max_frp_mw": max((f["frp_mw"] or 0 for f in fires), default=None),
                    "max_conflict_score": max((f["conflict_score"] for f in fires
                                               if f["conflict_score"] is not None),
                                              default=None)}
    quakes = []
    for id_, us, la, lo_, val, attrs in _obs_near(runtime, "quake", subj, lo, hi, r * 3):
        a = _attrs(attrs)
        quakes.append({"id": id_, "ts": _us_iso(us), "lat": la, "lon": lo_, "mag": val,
                       "depth_km": a.get("depth_km"), "place": a.get("place"),
                       "event_type": a.get("event_type"), "url": a.get("url"),
                       **geo(lat, lon, la, lo_)})
    quakes.sort(key=lambda q: q["distance_km"])
    out["quakes"] = {"count": len(quakes), "items": quakes[:20]}
    hazards = []
    for id_, us, la, lo_, val, attrs in _obs_near(runtime, "hazard", subj,
                                                   lo - timedelta(days=5), hi, r * 4):
        a = _attrs(attrs)
        hazards.append({"id": id_, "ts": _us_iso(us), "lat": la, "lon": lo_, "value": val,
                        "source": a.get("source"), "hazard": a.get("hazard"),
                        "name": a.get("name"), "alert_level": a.get("alert_level"),
                        "url": a.get("url"), **geo(lat, lon, la, lo_)})
    hazards.sort(key=lambda h: h["distance_km"])
    out["hazards"] = {"count": len(hazards), "items": hazards[:20]}
    return out


def _flag_tracks(runtime, tracks: list[dict]) -> None:
    for fn in hooks.TRACK_FLAGGERS:
        try:
            fn(runtime, tracks)
        except Exception:
            log.exception("track flagger %r failed", fn)


def section_traffic(runtime, subj: dict, now: datetime) -> dict:
    """Aircraft / ships near the point now; for events also tracks that passed within the
    radius from 3 h before to 3 h after the event start (closest approach)."""
    lat, lon, r = subj["lat"], subj["lon"], max(subj["radius_km"], 25.0)
    live = []
    with runtime.ingestor._lock:
        positions = list(runtime.ingestor.live.values())
    cutoff = now - timedelta(minutes=30)
    for p in positions:
        if p.ts < cutoff or abs(p.lat - lat) > r / 100.0 + 0.5:
            continue
        d = haversine_km(lat, lon, p.lat, p.lon)
        if d <= r and p.track_id != subj.get("id"):
            live.append({"id": p.track_id, "kind": p.kind, "ts": p.ts.isoformat(),
                         "lat": p.lat, "lon": p.lon, "alt_ft": p.alt_ft,
                         "speed_kts": p.speed_kts, "heading_deg": p.heading_deg,
                         **geo(lat, lon, p.lat, p.lon)})
    passed = []
    if subj["kind"] == "event":
        lo, hi = _window(subj, now, 3, 3)
        w, s, e, n = bbox_around(lat, lon, r)
        lon_sql = "lon BETWEEN ? AND ?" if w <= e else "(lon >= ? OR lon <= ?)"
        k = math.cos(math.radians(lat))
        sql = ("SELECT track_id, kind, arg_min(epoch_us(ts), d), min(d), count(*), "
               "arg_min(lat, d), arg_min(lon, d) FROM (SELECT track_id, kind, ts, lat, lon, "
               f"sqrt(power(lat - ?, 2) + power((lon - ?) * {k:.6f}, 2)) * 111.0 AS d "
               f"FROM positions WHERE ts >= ? AND ts <= ? AND lat BETWEEN ? AND ? AND {lon_sql}) "
               "GROUP BY track_id, kind ORDER BY min(d) LIMIT 60")
        try:
            rows = runtime.lake.query(sql, [lat, lon, lo, hi, s, n, w, e], start=lo, end=hi)
        except Exception:  # noqa: BLE001 — no positions partitions for that window
            rows = []
        passed = [{"id": tid, "kind": kind, "ts": _us_iso(us), "closest_km": round(d, 2),
                   "fixes": cnt, "lat": la, "lon": lo_} for tid, kind, us, d, cnt, la, lo_
                  in rows if d <= r]
    ids = list({x["id"] for x in live + passed})
    meta = runtime.store.get_tracks(ids) if ids else {}
    for x in live + passed:
        m = meta.get(x["id"]) or {}
        x.update({"name": m.get("name"), "type_code": m.get("type_code"),
                  "operator": m.get("operator"), "is_mil": bool(m.get("is_mil")),
                  "high_priority": bool(m.get("high_priority")), "flags": []})
    _flag_tracks(runtime, live + passed)
    live.sort(key=lambda x: (not x["is_mil"], x["distance_km"]))
    count = lambda xs, kind: sum(1 for x in xs if x["kind"] == kind)
    return {"radius_km": r,
            "now": {"aircraft": count(live, "aircraft"), "ships": count(live, "ship"),
                    "military": sum(1 for x in live if x["is_mil"]), "items": live[:40]},
            "passed": {"window": None if subj["kind"] != "event" else
                       dict(zip(("from", "to"), map(_iso, _window(subj, now, 3, 3)),
                                strict=True)),
                       "military": sum(1 for x in passed if x["is_mil"]),
                       "items": passed[:40]}}


def _frontline_km(runtime, lat: float, lon: float) -> dict | None:
    from osint.fires import frontline
    rec = frontline.load(runtime.settings.data_dir / "reference")
    if not rec:
        return None
    k = math.cos(math.radians(lat))
    best = None
    for line in rec.get("lines") or []:
        for i in range(len(line) - 1):
            (x1, y1), (x2, y2) = line[i], line[i + 1]
            ax, ay = (x1 - lon) * k, y1 - lat
            bx, by = (x2 - lon) * k, y2 - lat
            dx, dy = bx - ax, by - ay
            L = dx * dx + dy * dy
            tt = 0.0 if L == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L))
            px, py = ax + tt * dx, ay + tt * dy
            dist = math.hypot(px, py) * 111.0
            if best is None or dist < best[0]:
                best = (dist, lat + py, lon + px / k if k else lon)
    if best is None:
        return None
    return {"distance_km": round(best[0], 1), "lat": round(best[1], 5),
            "lon": round(best[2], 5), "source": rec.get("source") or "DeepStateMap",
            "source_datetime": rec.get("source_datetime"),
            "compass": compass(bearing_deg(lat, lon, best[1], best[2]))}


def section_assets(runtime, subj: dict, now: datetime) -> dict:
    """Reference assets within ~100 km (chokepoints 600 km), nearest first."""
    from osint.reference.chokepoints import chokepoint_features
    from osint.reference.loader import get_reference
    from osint.reference.nuclear import get_nuclear

    lat, lon = subj["lat"], subj["lon"]
    r = max(100.0, subj["radius_km"])
    bb = bbox_around(lat, lon, r)
    ref = get_reference()
    out: list[dict] = []

    def add(typ: str, items: list[dict], name_key: str = "name", limit: int = 8,
            max_km: float = r, **extra) -> None:
        rows = []
        for it in items:
            g = geo(lat, lon, it.get("lat"), it.get("lon"))
            if g["distance_km"] is None or g["distance_km"] > max_km:
                continue
            rows.append({"type": typ, "name": it.get(name_key) or it.get("name"),
                         "lat": it.get("lat"), "lon": it.get("lon"), **g,
                         **{k: it.get(k) for k in extra.get("keep", ())}})
        rows.sort(key=lambda x: x["distance_km"])
        out.extend(rows[:limit])

    add("base", ref.query_bases(bb, None), keep=("type", "country", "operator"))
    add("airport", ref.query_airports(bb, "medium", False),
        keep=("type", "icao", "iata", "is_military"))
    add("port", ref.query_ports(bb), keep=("harbor_size",))
    add("nuclear", get_nuclear().query(bb, None), keep=("type", "status", "operator", "id"))
    add("chokepoint", chokepoint_features(), limit=3, max_km=600.0, keep=("note",))
    for fn in hooks.ASSET_PROVIDERS:
        try:
            extra = fn(runtime, lat, lon, r) or []
        except Exception:
            log.exception("asset provider %r failed", fn)
            continue
        for it in extra:
            out.append({**it, **geo(lat, lon, it.get("lat"), it.get("lon"))})
    webcams = []
    try:
        from osint.webcams.catalogue import get_catalogue
        webcams = get_catalogue().near(lat, lon, 25, 6)
    except Exception:
        log.debug("webcams unavailable", exc_info=True)
    out.sort(key=lambda x: (x["distance_km"] is None, x["distance_km"] or 0))
    return {"radius_km": r, "items": out, "frontline": _frontline_km(runtime, lat, lon),
            "webcams": {"count": len(webcams), "items": webcams}}


def section_imagery(runtime, subj: dict, now: datetime) -> dict | None:
    if subj["kind"] != "event":
        return None
    from osint.api.imagery import imagery_payload
    from osint.imagery.config import ImageryConfig
    from osint.imagery.job import get_store
    cfg_dir = getattr(runtime.settings, "config_dir", None)
    cfg = ImageryConfig.load(cfg_dir / "imagery.yaml") if cfg_dir else ImageryConfig()
    return imagery_payload(runtime, get_store(runtime), cfg, subj["id"])


def repost_groups(docs: list[dict]) -> list[list[int]]:
    """Indexes of documents grouped by near-identical title (a Telegram repost of an
    article, the same wire story twice): token Jaccard >= 0.7, first published leads."""
    toks = [_tokens(d.get("title_en") or d.get("title")) for d in docs]
    groups: list[list[int]] = []
    for i, t in enumerate(toks):
        for g in groups:
            a = toks[g[0]]
            if t and a and len(t & a) / len(t | a) >= 0.7:
                g.append(i)
                break
        else:
            groups.append([i])
    return groups


def section_sources(runtime, subj: dict, now: datetime) -> dict | None:
    ev = subj.get("event")
    if not ev:
        return None
    docs = sorted(ev.get("documents") or [], key=lambda d: d.get("published_at") or "")
    groups = repost_groups(docs)
    langs = sorted({(d.get("lang") or d.get("language") or "").split("-")[0] for d in docs} - {""})
    kinds: dict[str, int] = {}
    for d in docs:
        kinds[source_kind(d.get("source"))] = kinds.get(source_kind(d.get("source")), 0) + 1
    return {"count": len(docs), "outlets": len({d.get("source") for d in docs}),
            "languages": langs, "by_kind": kinds, "groups": groups}


def section_entities(runtime, subj: dict, now: datetime) -> dict:
    """Actors and places to pivot on (from the event record, its extracted fields and
    impact sites; for points, the most frequent places of nearby events)."""
    ents: dict[tuple, dict] = {}

    def add(typ: str, name: str | None, **kw) -> None:
        name = (name or "").strip()
        if not name or len(name) > 120:
            return
        k = (typ, name.casefold())
        if k in ents:
            ents[k]["count"] += 1
        else:
            ents[k] = {"type": typ, "name": name, "count": 1, **kw}

    ev = subj.get("event")
    if ev:
        for a in ev.get("actors") or []:
            if isinstance(a, dict):
                add("actor", a.get("name"), role=a.get("role"), code=a.get("type"))
        for f in ev.get("fields") or []:
            # extracted actor values are often descriptions ("a large-scale attack by
            # Ukrainian drones"): only short, capitalised names make useful pivots
            v = str(f.get("value") or "").strip()
            if f.get("field") in ("actor", "actors") and v[:1].isupper() and len(v.split()) <= 5:
                add("actor", v, basis="extracted")
        if ev.get("place_name"):
            add("place", ev["place_name"], lat=ev.get("lat"), lon=ev.get("lon"),
                country=ev.get("country"))
        for s in ((ev.get("impact") or {}).get("sites") or []):
            add("site", s.get("name"), lat=s.get("lat"), lon=s.get("lon"),
                precision=s.get("precision"))
    elif subj.get("lat") is not None:
        rows = runtime.store.list_events(bbox=bbox_around(subj["lat"], subj["lon"],
                                                          subj["radius_km"]),
                                         since=now - timedelta(days=30), limit=500,
                                         include_unlocated=False, origin="news")
        for x in rows:
            add("place", x.get("place_name"), lat=x.get("lat"), lon=x.get("lon"),
                country=x.get("country"))
        acts = runtime.store.event_actors([x["id"] for x in rows])
        for lst in acts.values():
            for a in lst:
                if isinstance(a, dict):
                    add("actor", a.get("name"), code=a.get("type"))
    items = sorted(ents.values(), key=lambda e: (-e["count"], e["type"], e["name"]))
    return {"items": items[:30]}


def section_storyline(runtime, subj: dict, now: datetime) -> dict:
    """Storyline context (worker-derived, osint.storylines): for an event its thread
    (role, span by best-known event time, members); otherwise threads around the point (or
    naming the actor) with activity in the last 30 days, latest first."""
    from osint.storylines.store import get_store

    st = get_store(runtime)
    if subj["kind"] == "event":
        eid = (subj.get("event") or {}).get("id") or subj.get("id") or ""
        m = st.for_events([eid]).get(eid)
        if m is None:
            return {"member": None}
        s = st.get(m["storyline_id"]) or {}
        keep = ("event_id", "role", "role_basis", "title", "t", "t_basis", "t_precision",
                "category", "place_name", "lat", "lon", "origin")
        return {"member": m, "storyline": {
            k: s.get(k) for k in ("id", "title", "title_basis", "theatre_label", "t_first",
                                  "t_first_basis", "t_last", "n_events", "places", "roles")},
            "members": [{k: x.get(k) for k in keep} for x in s.get("members") or []]}
    since = now - timedelta(days=30)
    if subj["kind"] == "actor":
        items = st.list(q=subj.get("q"), since=since, limit=10)
    else:
        items = st.list(bbox=bbox_around(subj["lat"], subj["lon"], subj["radius_km"] * 2),
                        since=since, limit=10)
    keep = ("id", "title", "theatre_label", "t_first", "t_first_basis", "t_last", "n_events",
            "lat", "lon")
    return {"member": None, "nearby": [{k: s.get(k) for k in keep} for s in items]}


def section_assessment(runtime, subj: dict, now: datetime, ctx: dict) -> dict | None:
    """Known / corroborated / unconfirmed statements, each with its data basis."""
    ev = subj.get("event")
    if not ev:
        return None
    out: list[dict] = []

    def say(level: str, text: str, basis: str) -> None:
        out.append({"level": level, "text": text, "basis": basis})

    docs = ev.get("documents") or []
    outlets = len({d.get("source") for d in docs})
    say("known", f"Status {ev.get('status')} · confidence {float(ev.get('confidence') or 0):.2f}",
        "event record")
    if outlets <= 1:
        say("unconfirmed", "Reported by a single outlet", "documents")
    else:
        groups = repost_groups(sorted(docs, key=lambda d: d.get("published_at") or ""))
        indep = len(groups)
        txt = f"Reported by {outlets} outlets"
        if indep < len(docs):
            txt += f" ({indep} independent text{'s' if indep != 1 else ''}; others repost)"
        say("known" if indep > 1 else "unconfirmed", txt, "documents")
    v = ev.get("verification") or {}
    if v.get("verdict") == "supported":
        q = v.get("quotes") or []
        nv = sum(1 for x in q if x.get("verbatim"))
        say("known", f"Extracted facts match the source text ({nv}/{len(q)} quotes verbatim)",
            "extraction verification")
    elif v.get("verdict"):
        say("caveat", f"Extraction verdict {v['verdict']}"
            + (": " + "; ".join(map(str, v.get("reasons") or [])) if v.get("reasons") else ""),
            "extraction verification")
    corr = ev.get("corroborations") or []
    by_kind: dict[str, list] = {}
    for c in corr:
        by_kind.setdefault(c.get("kind"), []).append(c)
    t_first = min((parse_ts(d.get("published_at")) for d in docs if d.get("published_at")),
                  default=None)
    for kind, rows in by_kind.items():
        dists = [c["distance_km"] for c in rows if c.get("distance_km") is not None]
        ts = [parse_ts((c.get("details") or {}).get("ts")) for c in rows]
        ts = [t for t in ts if t]
        txt = f"{len(rows)} {kind} detection{'s' if len(rows) != 1 else ''}"
        if dists:
            txt += f" within {max(dists):.1f} km"
        if ts and t_first:
            dm = (min(ts) - t_first).total_seconds() / 60
            txt += f", earliest {abs(dm):.0f} min {'before' if dm < 0 else 'after'} first report"
        if kind == "fire":
            frp = max(float((c.get("details") or {}).get("frp_mw") or 0) for c in rows)
            txt += f" (max FRP {frp:.0f} MW)"
        say("corroborated", txt, f"sensor corroboration · {kind}")
    if not corr:
        say("unconfirmed", "No sensor corroboration linked", "corroboration")
    imp = ev.get("impact") or {}
    best = imp.get("best")
    if best and best.get("lat") is not None:
        say("known", f"Impact located to {best.get('name')} ({best.get('precision')})",
            "impact resolver")
    else:
        say("caveat", f"Location precision {ev.get('precision')} (place centroid: "
            f"{ev.get('place_name') or ev.get('country')})", "geocoder")
    for a in imp.get("areas") or []:
        if a.get("kind") == "firms_hull":
            ring = ((a.get("geometry") or {}).get("coordinates") or [[]])[0]
            if ring and ev.get("lat") is not None:
                clat = sum(p[1] for p in ring) / len(ring)
                clon = sum(p[0] for p in ring) / len(ring)
                g = geo(ev["lat"], ev["lon"], clat, clon)
                say("corroborated", f"Hotspot cluster {g['distance_km']:.1f} km {g['compass']} "
                    "of the reported location", a.get("basis") or "FIRMS hull")
    im = ctx.get("imagery") or {}
    after = next((a for a in im.get("assets") or [] if a.get("role") == "after"), None)
    if after and after.get("status") == "ok":
        say("corroborated", f"Post-event satellite image {str(after.get('acquired_at'))[:10]}",
            "imagery")
    elif im:
        say("unconfirmed", "No post-event satellite image yet"
            + (f" ({after['error']})" if after and after.get("error") else ""), "imagery")
    tl = ctx.get("timeline") or {}
    occ = tl.get("occurred") or {}
    if occ.get("conflicts"):
        say("caveat", "Sources disagree on when it happened", "timeline")
    elif occ.get("basis") == "claims":
        prec = {"minute": "stated time", "time_local": "stated local time ±1 h",
                "part_of_day": "date + part of day", "date": "date only",
                "range": "stated range"}.get(occ.get("precision"), occ.get("precision"))
        say("known", f"Occurred {occ['lo'][:16].replace('T', ' ')} → "
            f"{occ['hi'][:16].replace('T', ' ')} UTC ({occ.get('qualifier') or prec}, {prec})"
            if occ.get("qualifier") else
            f"Occurred {occ['lo'][:16].replace('T', ' ')} → {occ['hi'][:16].replace('T', ' ')} "
            f"UTC ({prec})", "timeline")
    else:
        say("unconfirmed", "No stated occurrence time; only before first report", "timeline")
    order = {"known": 0, "corroborated": 1, "unconfirmed": 2, "caveat": 3}
    out.sort(key=lambda x: order.get(x["level"], 9))
    return {"items": out}


def section_timeline(runtime, subj: dict, now: datetime, ctx: dict) -> dict | None:
    ev = subj.get("event")
    if not ev:
        return None
    from osint.imagery.job import get_store
    return build_timeline(ev, corroborations=ev.get("corroborations") or [],
                          imagery=get_store(runtime).get_assets(ev["id"]),
                          status_history=runtime.store.status_history(ev["id"]))


SIMPLE = {"related": section_related, "history": section_history,
          "sensors": section_sensors, "traffic": section_traffic, "assets": section_assets,
          "imagery": section_imagery, "sources": section_sources,
          "entities": section_entities, "storyline": section_storyline}
DEPENDENT = {"timeline": section_timeline, "assessment": section_assessment}


def run_section(name: str, runtime, subj: dict, now: datetime, ctx: dict | None = None):
    try:
        if name in DEPENDENT:
            return DEPENDENT[name](runtime, subj, now, ctx or {})
        if subj["kind"] == "actor" and name not in ("related", "entities", "storyline"):
            return None
        return SIMPLE[name](runtime, subj, now)
    except Exception as exc:
        log.exception("intel section %s failed", name)
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}
