"""Indicator extraction: pure functions over fetched rows, plus the time-windowed fetchers."""

import json
import logging
import re
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from osint.core.geo import haversine_km
from osint.naval import legs as L
from osint.naval import msi
from osint.naval.geo import Geo, mean_point
from osint.naval.models import Evidence

log = logging.getLogger(__name__)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _ts(us: int) -> datetime:
    return _EPOCH + timedelta(microseconds=int(us))


# ------------------------------------------------------------------------ air

def air_indicators(rows: list[tuple], metas: dict[str, dict], geo: Geo, cfg, *,
                   window_start: datetime, now: datetime) -> list[Evidence]:
    """rows: (track_id, ts_us, lat, lon, alt_ft, speed_kts, heading_deg)."""
    fl = {**cfg.sec("flights"), **cfg.sec("geo")}
    ctypes = cfg.sec("carrier_types")
    orb = cfg.sec("orbits")
    far = float(cfg.sec("geo").get("far_from_land_km", 150))
    by_track: dict[str, list[L.Pt]] = defaultdict(list)
    for tid, ts_us, la, lo, alt, spd, hdg in rows:
        by_track[tid].append(L.Pt(_ts(ts_us), la, lo, alt, spd, hdg))
    out: list[Evidence] = []
    gap = timedelta(minutes=float(fl.get("max_gap_min", 20)))
    grace = timedelta(minutes=float(fl.get("live_grace_min", 20)))
    for tid, pts in by_track.items():
        meta = metas.get(tid) or {}
        legs = L.segment_legs(tid, pts, window_start=window_start, now=now, max_gap=gap,
                              live_grace=grace)
        out += L.sea_terminating(legs, meta, geo, fl, ctypes)
        out += L.cvw_over_ocean(tid, pts, meta, geo, fl, ctypes)
        for leg in legs:
            ev = L.orbit(leg, meta, geo, orb, far)
            if ev is not None:
                out.append(ev)
    return out


# ---------------------------------------------------------------------- ships

def support_ship_match(meta: dict, cfg: dict) -> str | None:
    """'named' (fleet support name), 'military' (AIS type 35) or None."""
    name = " ".join((meta.get("name") or "").upper().replace(".", " ").split())
    if name:
        for pat in cfg.get("name_patterns") or []:
            if re.search(pat, name):
                return "named"
        bare = re.sub(r"^(USNS|RFA|USS)\s+", "", name)
        if bare in {n.upper() for n in cfg.get("support_names") or []}:
            return "named"
    if str(meta.get("type_code") or "") == "35" or meta.get("is_mil"):
        return "military"
    return None


def ship_indicators(rows: list[tuple], metas: dict[str, dict], geo: Geo, cfg, *,
                    now: datetime) -> list[Evidence]:
    """Support / military ships loitering in open water. rows like air_indicators."""
    sc = cfg.sec("ships")
    open_km = float(cfg.sec("geo").get("open_water_km", 40))
    span = timedelta(hours=float(sc.get("loiter_hours", 12)))
    by_track: dict[str, list[tuple]] = defaultdict(list)
    for tid, ts_us, la, lo, _alt, spd, _hdg in rows:
        by_track[tid].append((_ts(ts_us), la, lo, spd))
    out = []
    for tid, pts in by_track.items():
        meta = metas.get(tid) or {}
        kind = support_ship_match(meta, sc)
        if kind is None:
            continue
        pts.sort()
        recent = [p for p in pts if p[0] >= pts[-1][0] - span]
        if len(recent) < int(sc.get("min_points", 3)):
            continue
        if recent[-1][0] - recent[0][0] < span * 0.5:
            continue  # not enough history to call it loitering
        lat, lon = mean_point([(p[1], p[2]) for p in recent])
        rad = max(haversine_km(lat, lon, p[1], p[2]) for p in recent)
        speeds = sorted(p[3] for p in recent if p[3] is not None)
        med = speeds[len(speeds) // 2] if speeds else 0.0
        if rad > float(sc.get("max_radius_km", 60)) or med >= float(sc.get("max_speed_kts", 12)):
            continue
        coast = geo.coast_km(lat, lon)
        if coast <= open_km:
            continue
        w = float(sc.get("named_weight" if kind == "named" else "military_type_weight", 1.0))
        out.append(Evidence(
            kind="support_ship", ts=recent[-1][0], lat=lat, lon=lon, weight=w,
            description=(f"{'fleet support ship' if kind == 'named' else 'AIS military vessel'} "
                         f"{meta.get('name') or tid} loitering (median {med:.0f} kn, radius "
                         f"{rad:.0f} km over {(recent[-1][0] - recent[0][0]).total_seconds() / 3600:.0f} h)"
                         f" ~{coast:.0f} km from land"),
            refs=[{"type": "track", "id": tid}], radius_km=rad, source_key=tid))
    return out


def candidate_ship_ids(sqlite_path: Path, cfg) -> dict[str, dict]:
    """Sea tracks that may be fleet support / military (read-only query of the main store)."""
    sc = cfg.sec("ships")
    names = [n.upper() for n in sc.get("support_names") or []]
    if not sqlite_path.is_file():
        return {}
    con = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        marks = ",".join("?" * len(names)) or "''"
        rows = con.execute(
            "SELECT track_id, name, type_code, category, is_mil, extra FROM tracks "
            "WHERE kind='sea' AND (is_mil=1 OR type_code='35' OR upper(name) LIKE 'USNS %' "
            f"OR upper(name) LIKE 'RFA %' OR upper(name) IN ({marks}))", names).fetchall()
    except sqlite3.Error as exc:
        log.warning("naval: ship candidate query failed: %s", exc)
        return {}
    finally:
        con.close()
    out = {}
    for r in rows:
        d = dict(r)
        try:
            d["extra"] = json.loads(d.get("extra") or "{}")
        except ValueError:
            d["extra"] = {}
        out[d["track_id"]] = d
    return {k: v for k, v in out.items() if support_ship_match(v, sc)}


# --------------------------------------------------------------- nav warnings

def warning_key(attrs: dict) -> str:
    return f"{attrs.get('nav_area')}-{attrs.get('msg_number')}-{attrs.get('msg_year')}"


def warning_indicators(rows: list[dict], active: set[str] | None, geo: Geo, cfg, *,
                       now: datetime) -> list[Evidence]:
    """rows: {id, url, title, attrs(dict), published_us}. ``active`` = keys still in force
    (None = unknown -> keep all not cancelled by date)."""
    nc = cfg.sec("nav_warnings")
    strong, medium = nc.get("strong_keywords") or [], nc.get("medium_keywords") or []
    max_r = float(nc.get("max_radius_km", 400))
    min_open = float(nc.get("min_open_ocean_km", 30))
    seen: set[str] = set()
    out = []
    for r in rows:
        a = r.get("attrs") or {}
        key = warning_key(a)
        if key in seen:
            continue
        seen.add(key)
        if active is not None and key not in active:
            continue
        text = a.get("text") or r.get("title") or ""
        cancel = msi.cancel_time(text)
        if cancel is not None and cancel < now:
            continue
        s, m = msi.keyword_flags(text, strong, medium)
        if not s and not m:
            continue
        geoms = a.get("geometries") or msi.parse_geometries(text)
        summ = msi.geometry_summary(geoms)
        if summ is None:
            continue
        lat, lon, radius = summ
        if radius > 3000:  # multi-area bulletin spread over an ocean basin: not localising
            continue
        if geo.coast_km(lat, lon) < min_open and radius < 60:
            continue  # small range right off a coast (routine firing range)
        polygon = next((g["coords"] for g in geoms if g["type"] == "polygon"), None)
        w = float(nc.get("strong_weight", 2.5) if s else nc.get("medium_weight", 1.0))
        out.append(Evidence(
            kind="nav_warning", ts=now, lat=lat, lon=lon, weight=w,
            description=(f"NGA {r.get('title') or key}: {', '.join(s + m)} "
                         f"(area radius ~{radius:.0f} km)"),
            refs=[{"type": "warning", "id": key, "doc_id": r.get("id"), "url": r.get("url")}],
            radius_km=min(max_r, max(radius, 20.0)), polygon=polygon,
            source_key="warn:" + key))
    return out


# ----------------------------------------------------------------------- news

class NameMatcher:
    def __init__(self, nc: dict) -> None:
        self.ctx = re.compile(nc.get("context_pattern") or "carrier", re.IGNORECASE)
        self.generic = [re.compile(g, re.IGNORECASE if not g.startswith("\\b") else 0)
                        for g in nc.get("generic_terms") or []]
        self.carriers = []
        for c in nc.get("carriers") or []:
            pats = [re.compile(r"(?<![\w])" + re.escape(a) + r"(?![\w])", re.IGNORECASE)
                    for a in c.get("aliases") or [c["name"]]]
            self.carriers.append((c["name"], pats, bool(c.get("context"))))

    def names(self, text: str) -> list[str]:
        has_ctx = bool(self.ctx.search(text))
        return [n for n, pats, need in self.carriers
                if (has_ctx or not need) and any(p.search(text) for p in pats)]

    def generic_hit(self, text: str) -> bool:
        return any(g.search(text) for g in self.generic) and bool(self.ctx.search(text))


def news_indicators(docs: list[dict], cfg, *, now: datetime) -> list[Evidence]:
    """docs: {id, url, title, snippet, ts(datetime), lat, lon, kind:'doc'|'event'}."""
    nc = cfg.sec("news")
    m = NameMatcher(nc)
    out = []
    for d in docs:
        if d.get("lat") is None or d.get("lon") is None:
            continue
        text = f"{d.get('title') or ''} {d.get('snippet') or ''}"
        names = m.names(text)
        if not names and not m.generic_hit(text):
            continue
        out.append(Evidence(
            kind="news", ts=d["ts"], lat=d["lat"], lon=d["lon"],
            weight=float(nc.get("weight", 0.3)),
            description=(f"news mention{': ' + ', '.join(names) if names else ' of carrier activity'}"
                         f" - '{(d.get('title') or '')[:120]}' (located at the story's place, "
                         "not the ship)"),
            refs=[{"type": d.get("kind", "doc"), "id": d["id"], "url": d.get("url"),
                   "title": (d.get("title") or "")[:160]}],
            radius_km=float(nc.get("radius_km", 600)), names=names,
            source_key="news:" + (names[0] if names else d["id"])))
    return out


# ----------------------------------------------------------------------- usni

def usni_prior(state: dict | None, cfg, *, now: datetime) -> list[Evidence]:
    uc = cfg.sec("usni")
    if not state or not state.get("entries"):
        return []
    try:
        as_of = datetime.fromisoformat(state["as_of"]).replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError):
        as_of = now
    out = []
    for e in state["entries"]:
        if e.get("lat") is None or e.get("status") != "at_sea":
            continue
        out.append(Evidence(
            kind="usni_prior", ts=as_of, lat=e["lat"], lon=e["lon"],
            weight=float(uc.get("prior_weight", 0.4)),
            description=(f"PRIOR (not an observation): USNI Fleet Tracker {state.get('as_of')} "
                         f"places {e['carrier']} in '{e.get('region')}' (coarse centroid)"),
            refs=[{"type": "usni", "id": e.get("hull") or e["carrier"], "url": state.get("url")}],
            radius_km=float(uc.get("prior_radius_km", 700)), names=[e["carrier"]],
            half_life_h=float(uc.get("prior_half_life_h", 96)),
            source_key="usni:" + e["carrier"]))
    return out
