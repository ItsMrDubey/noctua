"""Site context for the intel console: what is around a point beyond the platform's own
event stores. Each section follows the aggregate.py contract (plain dicts, ``{"error"}``
instead of raising) and states its basis and timestamp:

  environment  light at the event time and now (sun elevation / twilight phase, moon
               illumination; computed, osint.intel.astro), next sunrise / sunset, and the
               Open-Meteo weather at the site: current, the hour of the event (model data
               for the last 3 days) and the next 48 h (cloud cover, wind, precipitation)
  overhead     Sentinel-1/-2 and Landsat 8/9 passes whose footprint covers the site
               (osint.intel.passes, SGP4 on the cached CelesTrak elements): last before the
               event, first after it, next upcoming, with forecast cloud for optical passes
  signals      the worker's live signal snapshots at the site: GNSS interference cells,
               the country's IODA outage status, nearby radiation probes, space weather,
               emergency squawks nearby
  exposure     critical infrastructure (power plants, refineries, LNG, dams, ground stations,
               cable landings, nearest pipeline / submarine cable, small and military
               airfields) and settlement population (GeoNames cities1000) within 50 km
  activity     aircraft / ships recorded within the radius over the last 24 h (distinct
               tracks per hour, military, longest AIS reporting gaps) and watchlist hits
  country      the country's curated conflict record (UCDP GED, by month / type) and the
               platform's recent event tempo against its own baseline
  attention    reports per day attached to events around the point (own data)
  track        (aircraft / ship) the last 24 h of the track from the lake: path, distance,
               gaps (AIS dark periods), loitering and port calls, cables / pipelines passed,
               countries crossed, GNSS-degraded cells crossed, events and front line near the
               path, watchlist hits and alerts, live state; plus "alerts" (notable findings)
  fire         (fire) the conflict score with its reasons and features, the FIRMS detection

Outbound requests (Open-Meteo, Wikimedia pageviews) are cached in-process; the overhead
computation is cached per site and hour.
"""

import csv
import itertools
import json
import logging
import math
import os
import sqlite3
import statistics
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
import numpy as np

from osint.core.geo import haversine_km
from osint.intel import astro, passes
from osint.timeline.build import bearing_deg, compass, parse_ts

log = logging.getLogger(__name__)

SECTIONS = ("environment", "overhead", "signals", "exposure", "activity", "country",
            "attention", "track", "fire")
EXPOSURE_KM = 50.0
RINGS_KM = (10.0, 25.0, 50.0)
USER_AGENT = "noctua-osint/0.1 ({contact}; intel console site context) httpx/" + httpx.__version__
DEFAULT_CONTACT = "https://meta.wikimedia.org/wiki/User-Agent_policy; contact via OSINT_CONTACT"


def _user_agent() -> str:
    return USER_AGENT.format(contact=os.environ.get("OSINT_CONTACT") or DEFAULT_CONTACT)


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat(timespec="seconds")


def _geo(lat0: float, lon0: float, lat: float, lon: float) -> dict:
    b = bearing_deg(lat0, lon0, lat, lon)
    return {"distance_km": round(haversine_km(lat0, lon0, lat, lon), 1), "compass": compass(b)}


class TTLCache:
    """Small thread-safe dict with per-entry expiry and a size cap (oldest evicted)."""

    def __init__(self, ttl_s: float, max_items: int = 512, clock=time.monotonic) -> None:
        self.ttl, self.max, self.clock = ttl_s, max_items, clock
        self._d: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._d.get(key)
            if hit is None or self.clock() - hit[0] > self.ttl:
                return None
            return hit[1]

    def put(self, key, value) -> None:
        with self._lock:
            self._d[key] = (self.clock(), value)
            if len(self._d) > self.max:
                for k, _ in sorted(self._d.items(), key=lambda kv: kv[1][0])[: len(self._d) // 4]:
                    del self._d[k]


# ------------------------------------------------------------------ weather (Open-Meteo)

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
WX_CURRENT = ("temperature_2m", "relative_humidity_2m", "cloud_cover", "precipitation",
              "weather_code", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
              "visibility", "is_day")
WX_HOURLY = ("cloud_cover", "cloud_cover_low", "precipitation", "wind_speed_10m",
             "wind_direction_10m", "visibility", "temperature_2m")
WX_PAST_DAYS, WX_FORECAST_DAYS = 3, 4
# WMO weather interpretation codes (Open-Meteo docs), coarse labels
WMO = {0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog",
       48: "rime fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
       81: "rain showers", 82: "violent showers", 85: "snow showers", 86: "snow showers",
       95: "thunderstorm", 96: "thunderstorm, hail", 99: "thunderstorm, hail"}


class PointWeather:
    """Open-Meteo forecast at a point (0.05 deg grid snap), cached ``ttl_s`` per point."""

    def __init__(self, client: httpx.Client | None = None, ttl_s: float = 30 * 60) -> None:
        self._client = client
        self.cache = TTLCache(ttl_s, 256)
        self._locks: dict = {}
        self._lock = threading.Lock()
        self.requests = 0

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=12.0, headers={"User-Agent": _user_agent()})
        return self._client

    def get(self, lat: float, lon: float) -> dict:
        key = (round(lat * 20) / 20, round(lon * 20) / 20)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        with self._lock:
            lk = self._locks.setdefault(key, threading.Lock())
        with lk:  # one fetch per point even when two sections ask at once
            hit = self.cache.get(key)
            if hit is not None:
                return hit
            params = {"latitude": f"{key[0]:g}", "longitude": f"{key[1]:g}",
                      "current": ",".join(WX_CURRENT), "hourly": ",".join(WX_HOURLY),
                      "past_days": WX_PAST_DAYS, "forecast_days": WX_FORECAST_DAYS,
                      "wind_speed_unit": "kn", "timezone": "UTC"}
            self.requests += 1
            r = self._http().get(OPEN_METEO, params=params)
            r.raise_for_status()
            data = r.json()
            data["fetched_at"] = _iso(datetime.now(UTC))
            data["grid"] = list(key)
            self.cache.put(key, data)
            return data


def hourly_at(wx: dict, t: datetime) -> dict | None:
    """The hourly row covering ``t`` (UTC hour), or None outside the fetched range."""
    h = (wx or {}).get("hourly") or {}
    times = h.get("time") or []
    key = t.astimezone(UTC).strftime("%Y-%m-%dT%H:00")
    try:
        i = times.index(key)
    except ValueError:
        return None
    return {"time": times[i] + "Z", **{k: (h.get(k) or [None] * len(times))[i] for k in WX_HOURLY}}


def _hourly_slice(wx: dict, lo: datetime, hi: datetime) -> dict:
    h = (wx or {}).get("hourly") or {}
    times = h.get("time") or []
    a, b = lo.strftime("%Y-%m-%dT%H:00"), hi.strftime("%Y-%m-%dT%H:00")
    idx = [i for i, t in enumerate(times) if a <= t <= b]
    return {"time": [times[i] + "Z" for i in idx],
            **{k: [(h.get(k) or [None] * len(times))[i] for i in idx]
               for k in ("cloud_cover", "precipitation", "wind_speed_10m")}}


WEATHER = PointWeather()


TIMED = ("event", "fire", "quake", "hazard")


def _event_time(subj: dict) -> datetime | None:
    """The subject's own time (events and observations); places / points / tracks have
    none: resolve_subject fills ``t`` with "now" for them."""
    return parse_ts(subj.get("t")) if subj["kind"] in TIMED else None


def section_environment(runtime, subj: dict, now: datetime) -> dict:
    lat, lon = subj["lat"], subj["lon"]
    t_ev = _event_time(subj)
    t_ref = t_ev or now
    out: dict = {"light": {"now": astro.light_at(now, lat, lon)},
                 "sun_next": {k: _iso(v) for k, v in astro.sun_events(now, lat, lon).items()},
                 "basis": "sun / moon computed (NOAA solar position, Meeus lunar series)"}
    if t_ev is not None:
        out["light"]["event"] = astro.light_at(t_ev, lat, lon)
    try:
        wx = WEATHER.get(lat, lon)
    except (httpx.HTTPError, ValueError) as exc:
        out["weather"] = {"error": f"Open-Meteo: {type(exc).__name__}: {exc}"[:200]}
        return out
    cur = dict(wx.get("current") or {})
    cur["label"] = WMO.get(cur.get("weather_code"))
    nxt = _hourly_slice(wx, now, now + timedelta(hours=48))
    clouds = [c for c in nxt["cloud_cover"] if c is not None]
    winds = [w for w in nxt["wind_speed_10m"] if w is not None]
    out["weather"] = {
        "source": "Open-Meteo forecast API (model data; past hours are model analysis, "
                  "not observations)", "fetched_at": wx.get("fetched_at"), "grid": wx.get("grid"),
        "elevation_m": wx.get("elevation"), "current": cur,
        "at_event": hourly_at(wx, t_ref) if "event" in out["light"] else None,
        "next48": {"cloud_min": min(clouds, default=None), "cloud_max": max(clouds, default=None),
                   "cloud_mean": round(sum(clouds) / len(clouds)) if clouds else None,
                   "precip_mm": round(sum(p or 0 for p in nxt["precipitation"]), 1),
                   "wind_max_kn": max(winds, default=None)},
        "series": _hourly_slice(wx, now - timedelta(hours=24), now + timedelta(hours=48)),
    }
    if cur.get("wind_direction_10m") is not None:
        out["weather"]["downwind_deg"] = (cur["wind_direction_10m"] + 180) % 360
        out["weather"]["downwind"] = compass(out["weather"]["downwind_deg"])
    return out


# ------------------------------------------------------------------ overhead passes

PASS_CACHE = TTLCache(30 * 60, 256)


def _signal_reader(runtime):
    from osint.signals.cache import SignalReader

    r = getattr(runtime, "_intel_signal_reader", None)
    if r is None:
        r = SignalReader(runtime.settings.data_dir)
        try:
            runtime._intel_signal_reader = r
        except AttributeError:
            pass
    return r


def section_overhead(runtime, subj: dict, now: datetime) -> dict:
    lat, lon = subj["lat"], subj["lon"]
    sats = _signal_reader(runtime).get("satellites") or {}
    rows = passes.pick_satellites(sats.get("objects") or {})
    if not rows:
        return {"error": "no Earth-observation element sets cached (satellites collector)"}
    t_ev = _event_time(subj)
    if t_ev and t_ev < now - timedelta(days=10):
        t_ev = None  # too old for a useful "first pass after"; show the upcoming ones
    start = (t_ev - timedelta(days=3)) if t_ev else now - timedelta(days=2)
    end = now + timedelta(days=4)
    key = (round(lat, 2), round(lon, 2), start.strftime("%Y%m%d%H"), now.strftime("%Y%m%d%H"),
           sats.get("updated"))
    items = PASS_CACHE.get(key)
    if items is None:
        items = passes.predict(rows, lat, lon, start, end, now)
        PASS_CACHE.put(key, items)
    items = [dict(p) for p in items]
    wx = None
    try:
        wx = WEATHER.get(lat, lon)
    except (httpx.HTTPError, ValueError):
        log.debug("overhead: no cloud forecast", exc_info=True)
    for p in items:
        hr = hourly_at(wx, datetime.fromisoformat(p["t"])) if wx else None
        p["cloud_pct"] = hr.get("cloud_cover") if hr else None
    first = lambda xs: xs[0] if xs else None
    by_kind = {}
    for kind in ("optical", "sar"):
        xs = [p for p in items if p["kind"] == kind]
        tn = _iso(now)
        rec = {"next": first([p for p in xs if p["t"] >= tn]),
               "next_clear": first([p for p in xs if p["t"] >= tn and p.get("cloud_pct") is not None
                                    and p["cloud_pct"] < 30]) if kind == "optical" else None,
               "last": first([p for p in reversed(xs) if p["t"] < tn])}
        if t_ev:
            te = _iso(t_ev)
            rec["before_event"] = first([p for p in reversed(xs) if p["t"] < te])
            rec["after_event"] = first([p for p in xs if p["t"] >= te])
        by_kind[kind] = rec
    ages = [p["element_age_h"] for p in items if p.get("element_age_h") is not None]
    return {"window": {"from": _iso(start), "to": _iso(end)}, "event_t": _iso(t_ev),
            "satellites": [r[0][1] for r in rows], "passes": items, "by_kind": by_kind,
            "elements_updated": sats.get("updated"),
            "max_element_age_h": max(ages, default=None),
            "basis": "SGP4 on CelesTrak GP elements; footprint geometry only. Actual "
                     "acquisition depends on each mission's plan and satellite status; "
                     "cloud is the Open-Meteo hourly forecast / model value at the pass hour."}


# ------------------------------------------------------------------ signals at the site

def section_signals(runtime, subj: dict, now: datetime) -> dict:
    from osint.api.signals import radiation_items

    lat, lon = subj["lat"], subj["lon"]
    r = _signal_reader(runtime)
    out: dict = {}
    g = r.get("gnss") or {}
    cells = []
    for c in g.get("cells") or []:
        d = haversine_km(lat, lon, c["lat"], c["lon"])
        if d <= 300:
            cells.append({"h3": c["h3"], "level": c["level"], "pct": c["pct"], "n": c["n"],
                          "bad": c["bad"], "lat": c["lat"], "lon": c["lon"], **_geo(lat, lon, c["lat"], c["lon"])})
    cells.sort(key=lambda c: c["distance_km"])
    near = [c for c in cells if c["distance_km"] <= 120]  # res-3 cells are ~70 km across
    out["gnss"] = {"updated": g.get("updated"), "window_min": g.get("window_min"),
                   "cells": cells[:12], "site": near[0] if near else None,
                   "worst_150km": max((c for c in cells if c["distance_km"] <= 150),
                                      key=lambda c: c["pct"], default=None),
                   "thresholds": g.get("thresholds"),
                   "basis": "share of ADS-B aircraft reporting degraded NIC/NACp per H3 cell "
                            "(gpsjam method); no aircraft = no data, not 'clean'"}
    cc = subj.get("cc")
    o = r.get("outages") or {}
    if cc:
        row = next((c for c in o.get("countries") or [] if c.get("cc") == cc), None)
        evs = [e for e in o.get("events") or [] if e.get("cc") == cc]
        out["outage"] = {"cc": cc, "updated": o.get("updated"), "window_h": o.get("window_h"),
                         "country": row, "events": len(evs),
                         "datasources": sorted({e.get("datasource") for e in evs} - {None}),
                         "basis": "IODA outage events (BGP / active probing / telescope)"}
    rad = radiation_items(r.get("radiation"), r.get("radiation_baseline"))
    st = []
    for s in rad:
        d = haversine_km(lat, lon, s["lat"], s["lon"])
        if d <= 250:
            st.append({**{k: s.get(k) for k in ("id", "name", "source", "value", "unit", "ts",
                                                "baseline", "status", "lat", "lon")},
                       **_geo(lat, lon, s["lat"], s["lon"])})
    st.sort(key=lambda s: s["distance_km"])
    out["radiation"] = {"updated": (r.get("radiation") or {}).get("updated"), "stations": st[:6],
                        "count_250km": len(st), "basis": "BfS ODL / Safecast probes vs own median"}
    sw = r.get("spaceweather") or {}
    out["space"] = {"updated": sw.get("updated"), "kp_now": sw.get("kp_now"),
                    "scales": (sw.get("scales") or {}).get("now"), "solar_wind": sw.get("solar_wind"),
                    "basis": "NOAA SWPC"}
    sq = r.get("squawks") or {}
    near_sq = []
    for e in sq.get("items") or []:
        if e.get("lat") is None:
            continue
        d = haversine_km(lat, lon, e["lat"], e["lon"])
        if d <= 300:
            near_sq.append({k: e.get(k) for k in ("id", "code", "label", "flight", "reg", "type",
                                                  "first_seen", "last_seen", "lat", "lon")}
                           | _geo(lat, lon, e["lat"], e["lon"]))
    out["squawks"] = {"updated": sq.get("updated"), "items": near_sq[:10]}
    return out


# ------------------------------------------------------------------ settlements / countries

class Settlements:
    """GeoNames cities1000 places (lat, lon, population, country) in numpy arrays, loaded
    once from the extraction geocoder's ``cities1000.sqlite`` (deduplicated by name and
    position: the index holds one row per alternate name)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = db.execute("SELECT name, country, admin1, lat, lon, max(pop) FROM places "
                              "GROUP BY country, lat, lon").fetchall()
        finally:
            db.close()
        self.names = [r[0] for r in rows]
        self.cc = [r[1] for r in rows]
        self.lat = np.array([r[3] for r in rows], dtype=np.float64)
        self.lon = np.array([r[4] for r in rows], dtype=np.float64)
        self.pop = np.array([r[5] or 0 for r in rows], dtype=np.int64)

    def near(self, lat: float, lon: float, km: float) -> list[tuple[int, float]]:
        dlat = km / 111.0
        dlon = km / (111.0 * max(0.05, math.cos(math.radians(lat))))
        m = (np.abs(self.lat - lat) <= dlat) & (np.abs(((self.lon - lon + 180) % 360) - 180) <= dlon)
        idx = np.nonzero(m)[0]
        if not len(idx):
            return []
        p1, p2 = math.radians(lat), np.radians(self.lat[idx])
        a = np.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * np.cos(p2) * \
            np.sin(np.radians(self.lon[idx] - lon) / 2) ** 2
        d = 2 * 6371.0088 * np.arcsin(np.minimum(1, np.sqrt(a)))
        keep = d <= km
        return sorted(zip(idx[keep].tolist(), d[keep].tolist(), strict=True), key=lambda x: x[1])

    def exposure(self, lat: float, lon: float, rings: tuple[float, ...]) -> dict:
        """Settlement population per ring, with city districts folded into their city.

        GeoNames lists many districts / suburbs as populated places of their own beside the
        city (London and its boroughs), so a plain sum double-counts metropolitan areas. A
        place is not added when it lies within the urban radius (0.010 x sqrt(pop) km: ~30
        km for 9 M, ~17 km for 3 M, ~8 km for 650 k) of a place at least 5x larger that is counted."""
        near = self.near(lat, lon, max(rings))
        kept: list[tuple[int, float]] = []
        folded = 0
        for i, d in sorted(near, key=lambda x: -int(self.pop[x[0]])):
            p = int(self.pop[i])
            big = next((j for j, _ in kept if int(self.pop[j]) >= 5 * max(p, 1) and
                        haversine_km(float(self.lat[i]), float(self.lon[i]), float(self.lat[j]),
                                     float(self.lon[j])) <= 0.010 * math.sqrt(int(self.pop[j]))),
                       None)
            if big is None:
                kept.append((i, d))
            else:
                folded += 1
        return {"rings": {f"{int(k)}": int(sum(int(self.pop[i]) for i, d in kept if d <= k))
                          for k in rings},
                "places": len(kept), "folded": folded, "kept": kept}

    def country_at(self, lat: float, lon: float) -> str | None:
        """Country of the nearest settlement within 60 km (a cheap reverse geocode)."""
        near = self.near(lat, lon, 60.0)
        return self.cc[near[0][0]] if near else None


_settlements: dict[str, Settlements | None] = {}
_settle_lock = threading.Lock()


def settlements(runtime) -> Settlements | None:
    path = Path(runtime.settings.data_dir) / "gazetteer" / "cities1000.sqlite"
    with _settle_lock:
        if str(path) not in _settlements:
            try:
                _settlements[str(path)] = Settlements(path) if path.is_file() else None
            except sqlite3.Error:
                log.exception("cities1000 unreadable")
                _settlements[str(path)] = None
        return _settlements[str(path)]


# UCDP country names that the GeoNames short names do not prefix-match
UCDP_ALIASES = {"CD": ["DR Congo (Zaire)"], "CI": ["Ivory Coast"], "US": ["United States of America"],
                "BA": ["Bosnia-Herzegovina"], "CG": ["Congo"], "MK": ["Macedonia, FYR"],
                "SZ": ["Kingdom of eSwatini (Swaziland)"], "KH": ["Cambodia (Kampuchea)"],
                "RU": ["Russia (Soviet Union)"], "RS": ["Serbia (Yugoslavia)"],
                "YE": ["Yemen (North Yemen)"], "MM": ["Myanmar (Burma)"]}
_country_names: dict[str, dict] = {}


def country_names(runtime) -> dict[str, str]:
    """ISO alpha-2 -> GeoNames country name (countryInfo.txt)."""
    path = Path(runtime.settings.data_dir) / "gazetteer" / "countryInfo.txt"
    if str(path) not in _country_names:
        names = {}
        if path.is_file():
            with path.open(encoding="utf-8") as f:
                for row in csv.reader((ln for ln in f if not ln.startswith("#")), delimiter="\t"):
                    if len(row) > 4 and len(row[0]) == 2:
                        names[row[0]] = row[4]
        _country_names[str(path)] = names
    return _country_names[str(path)]


def resolve_cc(runtime, subj: dict) -> str | None:
    c = ((subj.get("event") or {}).get("country") or "").strip()
    if len(c) == 2 and c.isalpha():
        return c.upper()
    if subj.get("lat") is None:
        return None
    s = settlements(runtime)
    return s.country_at(subj["lat"], subj["lon"]) if s else None


# ------------------------------------------------------------------ infrastructure exposure

_line_boxes: dict[int, tuple] = {}


def _lines_near(items: list[dict], key: str, lat: float, lon: float, km: float,
                multi: bool) -> list[tuple[dict, float]]:
    """(item, distance km) for line features within ``km``: bbox prefilter (cached numpy
    arrays per dataset) then exact point-segment distance."""
    from osint.infra.geo import seg_dist_km

    ck = id(items)
    if ck not in _line_boxes or _line_boxes[ck][0] is not items:
        boxes = []
        for it in items:
            lines = it.get(key) or []
            pts = [p for ln in lines for p in ln] if multi else lines
            if pts:
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                boxes.append((min(xs), min(ys), max(xs), max(ys)))
            else:
                boxes.append((999.0, 999.0, 999.0, 999.0))
        if len(_line_boxes) > 8:
            _line_boxes.clear()
        _line_boxes[ck] = (items, np.array(boxes, dtype=np.float64).reshape(-1, 4))
    _, b = _line_boxes[ck]
    dlat = km / 111.0
    dlon = km / (111.0 * max(0.05, math.cos(math.radians(lat))))
    m = (b[:, 0] <= lon + dlon) & (b[:, 2] >= lon - dlon) & (b[:, 1] <= lat + dlat) & \
        (b[:, 3] >= lat - dlat)
    out = []
    for i in np.nonzero(m)[0].tolist():
        it = items[i]
        best = None
        for ln in (it.get(key) or []) if multi else [it.get(key) or []]:
            for a, c in itertools.pairwise(ln):
                if abs(a[0] - c[0]) > 180:
                    continue
                d = seg_dist_km(lat, lon, a, c)
                best = d if best is None or d < best else best
        if best is not None and best <= km:
            out.append((it, best))
    out.sort(key=lambda x: x[1])
    return out


def section_exposure(runtime, subj: dict, now: datetime) -> dict:
    from osint.infra.loader import get_infra

    lat, lon, R = subj["lat"], subj["lon"], EXPOSURE_KM
    inf = get_infra()
    out: dict = {"radius_km": R}

    def pts(items: list[dict], keep: tuple, limit: int = 8) -> dict:
        rows = []
        for it in items:
            if it.get("lat") is None:
                continue
            if abs(it["lat"] - lat) > R / 111 + 0.1:
                continue
            d = haversine_km(lat, lon, it["lat"], it["lon"])
            if d <= R:
                rows.append({"name": it.get("name"), "lat": it["lat"], "lon": it["lon"],
                             **{k: it.get(k) for k in keep}, **_geo(lat, lon, it["lat"], it["lon"])})
        rows.sort(key=lambda x: x["distance_km"])
        return {"count": len(rows), "items": rows[:limit]}

    summary = inf.summary()
    power = pts(inf.points("power"), ("fuel", "group", "mw", "owner", "id"), 12)
    by_fuel: dict[str, float] = {}
    total = 0.0
    for p in inf.points("power"):
        if abs(p["lat"] - lat) > R / 111 + 0.1:
            continue
        if haversine_km(lat, lon, p["lat"], p["lon"]) <= R:
            by_fuel[p["group"]] = round(by_fuel.get(p["group"], 0) + (p.get("mw") or 0), 1)
            total += p.get("mw") or 0
    power["mw_total"] = round(total)
    power["mw_by_fuel"] = dict(sorted(by_fuel.items(), key=lambda kv: -kv[1]))
    out["power"] = power
    out["refineries"] = pts(inf.points("refineries"), ("operator", "product"))
    out["lng"] = pts(inf.points("lng"), ("operator", "country"))
    out["dams"] = pts(inf.points("dams"), ("height_m", "operator"))
    out["ground_stations"] = pts(inf.points("ground_stations"), ("operator",))
    out["landings"] = pts(inf.points("landings"), ())
    pipes = _lines_near(inf.pipelines, "line", lat, lon, R, multi=False)
    out["pipelines"] = {"count": len(pipes), "items": [
        {"name": p.get("name"), "substance": p.get("substance"), "operator": p.get("operator"),
         "offshore": p.get("offshore"), "distance_km": round(d, 1)} for p, d in pipes[:6]]}
    cables = _lines_near(inf.cables, "lines", lat, lon, R, multi=True)
    out["cables"] = {"count": len(cables), "items": [
        {"name": c.get("name"), "id": c.get("id"), "distance_km": round(d, 1)} for c, d in cables[:6]]}
    af, _ = inf.query_airfields((lon - 1, lat - 1, lon + 1, lat + 1))
    af = [a | _geo(lat, lon, a["lat"], a["lon"]) for a in af]
    af = [a for a in af if a["distance_km"] <= R]
    af.sort(key=lambda a: (not a["is_military"], a["distance_km"]))
    out["airfields"] = {"count": len(af), "military": sum(1 for a in af if a["is_military"]),
                        "items": [{k: a.get(k) for k in ("name", "type", "is_military", "lat", "lon",
                                                         "distance_km", "compass")} for a in af[:6]]}
    out["datasets"] = {k: v.get("available") for k, v in summary.items()}
    s = settlements(runtime)
    if s is not None:
        ex = s.exposure(lat, lon, RINGS_KM)
        top = sorted(ex["kept"], key=lambda x: -int(s.pop[x[0]]))[:8]
        out["population"] = {
            "rings": ex["rings"], "places": ex["places"], "folded_districts": ex["folded"],
            "items": [{"name": s.names[i], "pop": int(s.pop[i]), "lat": float(s.lat[i]),
                       "lon": float(s.lon[i]), **_geo(lat, lon, float(s.lat[i]), float(s.lon[i]))}
                      for i, _ in top],
            "basis": "sum of GeoNames populated-place populations (places >= 1,000) within "
                     "the ring, districts folded into their city: a settlement-based "
                     "estimate, not a gridded population count"}
    return out


# ------------------------------------------------------------------ traffic, last 24 h

def _watch_db(runtime) -> sqlite3.Connection | None:
    path = Path(runtime.settings.data_dir) / "infra_watch.sqlite"
    if not path.is_file():
        return None
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)


def watch_flags(runtime, tracks: list[dict]) -> None:
    """TRACK_FLAGGER: watchlist hits of live tracks (the watch job's ``active`` table)."""
    db = _watch_db(runtime)
    if db is None or not tracks:
        return
    try:
        ids = list({t["id"] for t in tracks})
        rows = db.execute(f"SELECT track_id, severity, hits FROM active WHERE track_id IN "
                          f"({','.join('?' * len(ids))})", ids).fetchall()
    except sqlite3.Error:
        return
    finally:
        db.close()
    hits = {tid: (sev, json.loads(h or "[]")) for tid, sev, h in rows}
    for t in tracks:
        if t["id"] not in hits:
            continue
        sev, hs = hits[t["id"]]
        for h in hs[:2]:
            e = h.get("entry") or {}
            t.setdefault("flags", []).append({
                "kind": "watchlist", "severity": sev,
                "label": ", ".join(e.get("risk") or []) or e.get("name") or "watchlist",
                "source": (e.get("source") or "watchlist") + f" · matched on {h.get('matched_on')}"
                          f" ({h.get('strength')})"})


def section_activity(runtime, subj: dict, now: datetime) -> dict:
    from osint.intel.aggregate import bbox_around

    lat, lon = subj["lat"], subj["lon"]
    r = max(subj["radius_km"], 25.0)
    lo = now - timedelta(hours=24)
    w, s, e, n = bbox_around(lat, lon, r)
    lon_sql = "lon BETWEEN ? AND ?" if w <= e else "(lon >= ? OR lon <= ?)"
    k = math.cos(math.radians(lat))
    inner = (f"SELECT track_id, kind, ts, lat, lon, sqrt(power(lat - ?, 2) + power((lon - ?) * {k:.6f}, 2))"
             f" * 111.0 AS d FROM positions WHERE ts >= ? AND ts <= ? AND lat BETWEEN ? AND ? AND {lon_sql}")
    params = [lat, lon, lo, now, s, n, w, e]
    try:
        tracks = runtime.lake.query(
            "SELECT track_id, kind, count(*), min(epoch_us(ts)), max(epoch_us(ts)), min(d), "
            "max(gap) FROM (SELECT *, epoch_us(ts) - lag(epoch_us(ts)) OVER "
            f"(PARTITION BY track_id ORDER BY ts) AS gap FROM ({inner}) WHERE d <= {float(r)}) "
            "GROUP BY track_id, kind", params, start=lo, end=now)
        hours = runtime.lake.query(
            "SELECT kind, (epoch_us(ts) // 3600000000) AS h, count(DISTINCT track_id) FROM "
            f"({inner}) WHERE d <= {float(r)} GROUP BY 1, 2", params, start=lo, end=now)
    except Exception as exc:
        log.debug("activity query failed", exc_info=True)
        tracks, hours = [], []
        err = f"{type(exc).__name__}: {exc}"[:200]
    else:
        err = None
    h0 = int(lo.timestamp()) // 3600
    series = {"aircraft": [0] * 25, "ship": [0] * 25}
    kinds = {"air": "aircraft", "aircraft": "aircraft", "sea": "ship", "ship": "ship"}
    tracks = [(t[0], kinds.get(t[1], t[1]), *t[2:]) for t in tracks]
    hours = [(kinds.get(k, k), h, c) for k, h, c in hours]
    for kind, h, c in hours:
        i = int(h) - h0
        if kind in series and 0 <= i < 25:
            series[kind][i] = int(c)
    ids = [t[0] for t in tracks]
    meta = runtime.store.get_tracks(ids) if ids else {}
    items = []
    for tid, kind, cnt, first, last, dmin, gap in tracks:
        m = meta.get(tid) or {}
        items.append({"id": tid, "kind": kind, "fixes": int(cnt),
                      "first": datetime.fromtimestamp(first / 1e6, UTC).isoformat(timespec="seconds"),
                      "last": datetime.fromtimestamp(last / 1e6, UTC).isoformat(timespec="seconds"),
                      "closest_km": round(float(dmin), 1),
                      "max_gap_min": round(gap / 6e7) if gap else None,
                      "name": m.get("name"), "type_code": m.get("type_code"),
                      "operator": m.get("operator"), "is_mil": bool(m.get("is_mil")),
                      "high_priority": bool(m.get("high_priority")), "flags": []})
    from osint.intel.aggregate import _flag_tracks

    _flag_tracks(runtime, items)
    gaps = sorted([x for x in items if x["kind"] == "ship" and (x["max_gap_min"] or 0) >= 60],
                  key=lambda x: -x["max_gap_min"])
    notable = sorted([x for x in items if x["is_mil"] or x["high_priority"] or x["flags"]],
                     key=lambda x: (not x["flags"], not x["is_mil"], x["closest_km"]))
    alerts = []
    db = _watch_db(runtime)
    if db is not None:
        try:
            rows = db.execute("SELECT ts, kind, track_id, severity, title, sub, lat, lon FROM alerts "
                              "WHERE ts >= ? AND lat BETWEEN ? AND ? ORDER BY ts DESC LIMIT 200",
                              (_iso(lo), s, n)).fetchall()
            for ts, kind, tid, sev, title, sub, la, lo_ in rows:
                if la is not None and haversine_km(lat, lon, la, lo_) <= r:
                    alerts.append({"ts": ts, "kind": kind, "track_id": tid, "severity": sev,
                                   "title": title, "sub": sub, "lat": la, "lon": lo_})
        except sqlite3.Error:
            log.debug("watch alerts unreadable", exc_info=True)
        finally:
            db.close()
    if err:
        return {"error": err}
    cnt = lambda kind: sum(1 for x in items if x["kind"] == kind)
    return {"radius_km": r, "window": {"from": _iso(lo), "to": _iso(now)},
            "aircraft": cnt("aircraft"), "ships": cnt("ship"),
            "military": sum(1 for x in items if x["is_mil"]),
            "flagged": sum(1 for x in items if x["flags"]),
            "series": {"start": _iso(datetime.fromtimestamp(h0 * 3600, UTC)), **series},
            "notable": notable[:20], "gaps": gaps[:10], "watch_alerts": alerts[:20],
            "basis": "positions recorded by the platform's ADS-B / AIS collectors (coverage "
                     "varies; a reporting gap can be coverage, not a transponder off)"} \
        if not err or items else {"error": err}


# ------------------------------------------------------------------ country context

def section_country(runtime, subj: dict, now: datetime) -> dict:
    cc = subj.get("cc")
    if not cc:
        return {"error": "no country for this location"}
    name = country_names(runtime).get(cc)
    names = [x for x in [name, *UCDP_ALIASES.get(cc, [])] if x]
    hist = runtime.store.country_history(names)
    months: dict[str, dict] = {}
    by_type: dict[str, int] = {}
    tot_ev = tot_fat = 0
    for month, tv, ev, fat in hist:
        m = months.setdefault(month, {"events": 0, "fatalities": 0})
        m["events"] += ev
        m["fatalities"] += int(fat)
        by_type[tv] = by_type.get(tv, 0) + ev
        tot_ev += ev
        tot_fat += int(fat)
    since = now - timedelta(days=37)
    tempo_rows = runtime.store.country_tempo(cc, since)
    days: dict[str, int] = {}
    cats7: dict[str, int] = {}
    d7 = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    for day, cat, c in tempo_rows:
        days[day] = days.get(day, 0) + c
        if day >= d7:
            cats7[cat] = cats7.get(cat, 0) + c
    first = min(days) if days else None
    series = []
    for i in range(37):
        dd = (since + timedelta(days=i + 1)).strftime("%Y-%m-%d")
        series.append(days.get(dd, 0))
    last7 = sum(series[-7:])
    # baseline: days before the last 7 that the platform has data for (it may be younger
    # than 30 days); None when it has fewer than 3 such days
    base_days = [v for i, v in enumerate(series[:-7])
                 if first and (since + timedelta(days=i + 1)).strftime("%Y-%m-%d") >= first]
    base = statistics.mean(base_days) if len(base_days) >= 3 else None
    from osint.intel.aggregate import bbox_around
    from osint.storylines.store import get_store

    stories = []
    if subj.get("lat") is not None:
        try:
            for s in get_store(runtime).list(bbox=bbox_around(subj["lat"], subj["lon"], 400),
                                             since=now - timedelta(days=7), limit=8):
                stories.append({k: s.get(k) for k in ("id", "title", "theatre_label", "t_last",
                                                      "n_events", "lat", "lon")})
        except Exception:
            log.debug("storylines unavailable", exc_info=True)
    return {"cc": cc, "name": name,
            "history": {"names": names, "total": tot_ev, "fatalities_best": tot_fat,
                        "months": months, "by_type": by_type,
                        "basis": "UCDP GED (curated; candidate releases lag by weeks)"},
            "tempo": {"days": series, "end": _iso(now), "last7": last7,
                      "baseline_per_day": None if base is None else round(base, 2),
                      "baseline_days": len(base_days),
                      "ratio": None if not base else round((last7 / 7) / base, 2),
                      "first_day": first, "categories_7d": dict(sorted(cats7.items(),
                                                                       key=lambda kv: -kv[1])),
                      "basis": "platform events located in the country (news + sensor), by "
                               "first report day; baseline = mean per day before the last 7"},
            "storylines": stories}


# ------------------------------------------------------------------ attention (own data)

def section_attention(runtime, subj: dict, now: datetime) -> dict:
    from osint.intel.aggregate import bbox_around

    lat, lon = subj["lat"], subj["lon"]
    r = max(subj["radius_km"] * 2, 50.0)
    since = (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = runtime.store.document_volume(bbox_around(lat, lon, r), since)
    by_day = {d: (docs, outlets, langs, evs) for d, docs, outlets, langs, evs in rows}
    days = [(since + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(30)]
    docs = [by_day.get(d, (0, 0, 0, 0))[0] for d in days]
    outlets = [by_day.get(d, (0, 0, 0, 0))[1] for d in days]
    prior = [v for v in docs[:-3]]
    med = statistics.median(prior) if prior else 0
    last3 = sum(docs[-3:]) / 3
    return {"radius_km": r, "days": days, "documents": docs, "outlets": outlets,
            "last3_per_day": round(last3, 1), "baseline_median": med,
            "ratio": None if not med else round(last3 / med, 1),
            "basis": "reports attached to platform events within the radius (bbox), by "
                     "publication day; bounded by the platform's own collection start"}


# ------------------------------------------------------------------ Wikipedia pageviews

PAGEVIEWS = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{project}/all-access/user/{title}/daily/{start}/{end}"
PV_CACHE = TTLCache(6 * 3600, 512)


def pageviews(title: str, project: str = "en.wikipedia", now: datetime | None = None,
              client: httpx.Client | None = None) -> dict:
    """Daily user pageviews of one article over the last 60 days (Wikimedia REST, cached
    6 h) with the spike ratio of the last 3 days against the prior median."""
    now = now or datetime.now(UTC)
    key = (project, title)
    hit = PV_CACHE.get(key)
    if hit is not None:
        return hit
    end = now - timedelta(days=1)
    start = end - timedelta(days=59)
    url = PAGEVIEWS.format(project=project, title=quote(title.replace(" ", "_"), safe=""),
                           start=start.strftime("%Y%m%d"), end=end.strftime("%Y%m%d"))
    c = client or httpx.Client(timeout=10.0, headers={"User-Agent": _user_agent()})
    try:
        r = c.get(url)
    finally:
        if client is None:
            c.close()
    if r.status_code == 404:
        out = {"title": title, "project": project, "days": [], "views": [], "missing": True}
        PV_CACHE.put(key, out)
        return out
    r.raise_for_status()
    items = r.json().get("items") or []
    by = {it["timestamp"][:8]: int(it.get("views") or 0) for it in items}
    days = [(start + timedelta(days=i)).strftime("%Y%m%d") for i in range(60)]
    views = [by.get(d, 0) for d in days]
    prior = views[:-3]
    med = statistics.median(prior) if prior else 0
    last3 = sum(views[-3:]) / 3
    out = {"title": title, "project": project,
           "days": [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in days], "views": views,
           "last3_per_day": round(last3), "baseline_median": med,
           "ratio": None if not med else round(last3 / med, 2),
           "peak": max(views, default=0), "fetched_at": _iso(datetime.now(UTC)),
           "source": "Wikimedia REST pageviews (user agents; one day lag)"}
    PV_CACHE.put(key, out)
    return out


# ------------------------------------------------------------------ track (aircraft / ship)

KIND_NAME = {"air": "aircraft", "sea": "ship", "aircraft": "aircraft", "ship": "ship"}
EMERGENCY = {"7500": "Unlawful interference", "7600": "Radio failure", "7700": "General emergency"}
GAP_MIN = {"aircraft": 30, "ship": 60}  # a gap this long is reported (coverage or transponder)
LOITER_KTS, LOITER_MIN = 1.0, 60


def _sample(n: int, k: int) -> list[int]:
    return sorted({round(i * (n - 1) / max(1, k - 1)) for i in range(k)}) if n else []


def _ts(t: float) -> str:
    return _iso(datetime.fromtimestamp(t, UTC))


def section_track(runtime, subj: dict, now: datetime) -> dict | None:
    """The selected aircraft / ship over the last 24 h (lake positions) plus its live state,
    metadata, watchlist hits and the notable findings ("alerts") the console promotes."""
    tid = subj.get("id") or ""
    if subj["kind"] not in ("aircraft", "ship") or not tid:
        return None
    kind = KIND_NAME.get(tid.partition(":")[0]) or KIND_NAME[subj["kind"]]
    meta = runtime.store.get_tracks([tid]).get(tid) or {}
    extra = meta.get("extra") or {}
    out: dict = {"id": tid, "kind": kind,
                 "meta": {k: meta.get(k) for k in ("name", "reg", "type_code", "operator", "category",
                                                   "is_mil", "high_priority", "first_seen",
                                                   "last_seen")},
                 "extra": {k: extra.get(k) for k in ("desc", "role", "squawk", "callsign",
                                                     "destination", "imo", "flag", "eta")
                           if extra.get(k) is not None}}
    with runtime.ingestor._lock:
        p = runtime.ingestor.live.get(tid)
    if p is not None:
        out["live"] = {"ts": _iso(p.ts), "lat": p.lat, "lon": p.lon, "alt_ft": p.alt_ft,
                       "speed_kts": p.speed_kts, "heading_deg": p.heading_deg, "source": p.source,
                       "age_s": round((now - p.ts).total_seconds())}
    lo = now - timedelta(hours=24)
    try:
        rows = runtime.lake.query(
            "SELECT epoch_us(ts), lat, lon, alt_ft, speed_kts FROM positions WHERE track_id = ? "
            "AND ts >= ? AND ts <= ? ORDER BY ts", [tid, lo, now], start=lo, end=now)
    except Exception as exc:  # no partitions for the window
        log.debug("track history query failed", exc_info=True)
        rows = []
        out["error_history"] = f"{type(exc).__name__}: {exc}"[:200]
    pts = [(us / 1e6, la, lo_, alt, spd) for us, la, lo_, alt, spd in rows if la is not None]
    alerts: list[dict] = []
    sq = str(extra.get("squawk") or "")
    if sq in EMERGENCY:
        alerts.append({"level": "alert", "text": f"Squawk {sq}: {EMERGENCY[sq]}", "basis": "ADS-B"})
    out["fixes"] = len(pts)
    if pts:
        _track_history(runtime, out, pts, kind, lo, alerts)
    tr = [{"id": tid, "flags": []}]
    watch_flags(runtime, tr)
    out["flags"] = tr[0]["flags"]
    for f in out["flags"]:
        alerts.insert(0, {"level": "alert" if f.get("severity") == "high" else "warn",
                          "text": "Watchlist: " + f["label"], "basis": f["source"]})
    out["watch_alerts"] = []
    db = _watch_db(runtime)
    if db is not None:
        try:
            keys = ("ts", "kind", "severity", "title", "sub", "lat", "lon")
            out["watch_alerts"] = [dict(zip(keys, r, strict=True)) for r in db.execute(
                "SELECT ts, kind, severity, title, sub, lat, lon FROM alerts WHERE track_id = ? "
                "AND ts >= ? ORDER BY ts DESC LIMIT 20", (tid, _iso(now - timedelta(days=7))))]
        except sqlite3.Error:
            log.debug("watch alerts unreadable", exc_info=True)
        finally:
            db.close()
    for a in out["watch_alerts"][:3]:
        alerts.append({"level": "warn", "text": "Watch alert: " + str(a["title"]) + " ("
                       + str(a["ts"])[:16] + "Z)", "basis": "infrastructure watch job"})
    out["alerts"] = alerts
    return out


def _track_history(runtime, out: dict, pts: list[tuple], kind: str, lo: datetime,
                   alerts: list[dict]) -> None:
    n = len(pts)
    dist = sum(haversine_km(a[1], a[2], b[1], b[2]) for a, b in itertools.pairwise(pts))
    alts = [x[3] for x in pts if x[3] is not None]
    spds = [x[4] for x in pts if x[4] is not None]
    out["summary"] = {"first": _ts(pts[0][0]), "last": _ts(pts[-1][0]), "distance_km": round(dist),
                      "alt_max_ft": max(alts, default=None), "alt_min_ft": min(alts, default=None),
                      "speed_max_kts": round(max(spds), 1) if spds else None}
    out["path"] = [[round(pts[i][1], 4), round(pts[i][2], 4), int(pts[i][0])]
                   for i in _sample(n, 300)]
    gaps = []
    for a, b in itertools.pairwise(pts):
        m = (b[0] - a[0]) / 60
        if m >= GAP_MIN[kind]:
            gaps.append({"from": _ts(a[0]), "to": _ts(b[0]), "minutes": round(m),
                         "jump_km": round(haversine_km(a[1], a[2], b[1], b[2]), 1),
                         "lat": a[1], "lon": a[2], "lat2": b[1], "lon2": b[2]})
    gaps.sort(key=lambda g: -g["minutes"])
    out["gaps"] = gaps[:10]
    if kind == "ship" and gaps and gaps[0]["minutes"] >= 180:
        g = gaps[0]
        alerts.append({"level": "alert", "text": f"AIS gap {g['minutes'] // 60} h "
                       f"{g['minutes'] % 60} min ({g['jump_km']} km apart)",
                       "basis": "lake positions; a coverage gap looks the same"})
    samp = [pts[i] for i in _sample(n, 60)]
    st, names = settlements(runtime), country_names(runtime)
    crossed: list[dict] = []
    for t, la, lo_, *_ in samp:
        cc = st.country_at(la, lo_) if st else None
        key = cc or "-"
        if not crossed or crossed[-1]["cc"] != key:
            crossed.append({"cc": key, "name": names.get(cc, cc) if cc else "sea / remote",
                            "from": _ts(t)})
    out["regions"] = crossed[:20]
    out["gnss_crossed"] = _gnss_crossed(runtime, samp, lo)
    if any(c["level"] == "high" for c in out["gnss_crossed"]):
        w = out["gnss_crossed"][0]
        alerts.append({"level": "warn", "text": f"Crossed GNSS interference ({w['pct']}% of aircraft "
                       f"degraded) at {w['at'][11:16]}Z", "basis": "ADS-B NIC/NACp, last 24 h"})
    from osint.intel.aggregate import _frontline_km

    fls = [f for f in (_frontline_km(runtime, la, lo_) for _, la, lo_, *_ in samp[::3]) if f]
    out["frontline_min"] = min(fls, key=lambda f: f["distance_km"]) if fls else None
    if out["frontline_min"] and out["frontline_min"]["distance_km"] <= 25:
        alerts.append({"level": "warn", "text": f"Within {out['frontline_min']['distance_km']} km "
                       "of the front line", "basis": out["frontline_min"]["source"]})
    lats, lons = [x[1] for x in pts], [x[2] for x in pts]
    evs = runtime.store.list_events(bbox=(min(lons) - 0.4, min(lats) - 0.3, max(lons) + 0.4,
                                          max(lats) + 0.3), since=lo, limit=400,
                                    include_unlocated=False, origin="news,sensor")
    near = []
    for e in evs:
        d = min(haversine_km(e["lat"], e["lon"], la, lo_) for _, la, lo_, *_ in samp)
        if d <= 25:
            near.append({k: e.get(k) for k in ("id", "title", "category", "status", "t_start",
                                                "lat", "lon", "place_name")}
                        | {"distance_km": round(d, 1)})
    near.sort(key=lambda e: e["distance_km"])
    out["events_near"] = near[:12]
    if kind == "ship":
        out.update(_ship_behaviour(pts, alerts))


def _gnss_crossed(runtime, samp: list[tuple], lo: datetime) -> list[dict]:
    """H3 cells the path crossed whose 24 h ADS-B integrity share was medium / high."""
    try:
        import h3

        from osint.api.signals import gnss_history

        live_g = _signal_reader(runtime).get("gnss") or {}
        hist = gnss_history(runtime.lake, 24, live_g, (lo + timedelta(hours=24)).timestamp())
    except Exception:  # signals history absent
        log.debug("gnss history unavailable", exc_info=True)
        return []
    res = int(live_g.get("res") or 3)
    bad = {c.get("h3") or c.get("cell"): c for c in hist["cells"]
           if c.get("level") in ("medium", "high")}
    seen: dict = {}
    for t, la, lo_, *_ in samp:
        c = h3.latlng_to_cell(la, lo_, res)
        if c in bad and c not in seen:
            seen[c] = {"level": bad[c].get("level"), "pct": bad[c].get("pct"), "n": bad[c].get("n"),
                       "cell": c, "at": _ts(t), "lat": la, "lon": lo_}
    return sorted(seen.values(), key=lambda c: -(c["pct"] or 0))[:8]


def _ship_behaviour(pts: list[tuple], alerts: list[dict]) -> dict:
    """Loitering spells (<= 1 kn for >= 60 min), which were port calls (within 5 km of a
    World Port Index port) or near a submarine cable / pipeline (3 km), and the cables /
    pipelines the path passed within 5 km of."""
    from osint.infra.loader import get_infra
    from osint.reference.loader import get_reference

    inf = get_infra()
    spells, cur = [], []
    for x in pts:
        if x[4] is not None and x[4] <= LOITER_KTS:
            cur.append(x)
            continue
        if cur and (cur[-1][0] - cur[0][0]) / 60 >= LOITER_MIN:
            spells.append(cur)
        cur = []
    if cur and (cur[-1][0] - cur[0][0]) / 60 >= LOITER_MIN:
        spells.append(cur)

    def lines_near(la: float, lo_: float, km: float) -> list[tuple[dict, float]]:
        return (_lines_near(inf.cables, "lines", la, lo_, km, multi=True)
                + _lines_near(inf.pipelines, "line", la, lo_, km, multi=False))

    loiters = []
    for sp in spells:
        la = sum(x[1] for x in sp) / len(sp)
        lo_ = sum(x[2] for x in sp) / len(sp)
        try:
            ports = get_reference().query_ports((lo_ - 0.1, la - 0.07, lo_ + 0.1, la + 0.07))
        except Exception:  # reference data absent
            log.debug("ports unavailable", exc_info=True)
            ports = []
        port = min(((pt, haversine_km(la, lo_, pt["lat"], pt["lon"])) for pt in ports),
                   key=lambda x: x[1], default=None)
        rec = {"from": _ts(sp[0][0]), "to": _ts(sp[-1][0]),
               "minutes": round((sp[-1][0] - sp[0][0]) / 60), "lat": round(la, 4),
               "lon": round(lo_, 4),
               "port": {"name": port[0].get("name"), "distance_km": round(port[1], 1)}
               if port and port[1] <= 5 else None,
               "near_lines": [{"name": it.get("name"), "distance_km": round(d, 2)}
                              for it, d in lines_near(la, lo_, 3.0)[:3]]}
        loiters.append(rec)
        if rec["near_lines"] and not rec["port"]:
            ln = rec["near_lines"][0]
            alerts.append({"level": "alert", "text": f"Loitered {rec['minutes']} min within "
                           f"{ln['distance_km']} km of {ln['name']}",
                           "basis": "lake positions vs cable / pipeline layers"})
    along: dict[str, float] = {}
    for i in _sample(len(pts), 80):
        for it, d in lines_near(pts[i][1], pts[i][2], 5.0):
            nm = it.get("name") or it.get("id") or "unnamed"
            along[nm] = min(d, along.get(nm, 99.0))
    return {"loiters": loiters[:10], "port_calls": [x for x in loiters if x["port"]][:10],
            "lines_crossed": [{"name": k, "distance_km": round(v, 2)}
                              for k, v in sorted(along.items(), key=lambda kv: kv[1])][:10]}


# ------------------------------------------------------------------ fire score

def section_fire(runtime, subj: dict, now: datetime) -> dict | None:
    """The fire's conflict score (osint.fires: reasons, features, threshold) and the FIRMS
    detection record."""
    if subj["kind"] != "fire" or not subj.get("id"):
        return None
    from osint.fires.store import get_score_store

    thr = 50
    try:
        from osint.fires.config import load_config as fire_config

        cfg_dir = getattr(runtime.settings, "config_dir", None)
        if cfg_dir is not None:
            thr = fire_config(cfg_dir / "fire_score.yaml").threshold
    except Exception:  # default cut when the config is unreadable
        log.debug("fire config unreadable", exc_info=True)
    out: dict = {"id": subj["id"], "threshold": thr, "score": None}
    rec = get_score_store(runtime).get(subj["id"])
    if rec:
        out.update({"score": rec["score"], "reasons": rec["reasons"],
                    "features": rec.get("features"), "computed_at": rec.get("computed_at"),
                    "war_likely": rec["score"] >= thr})
    t = parse_ts(subj.get("t")) or now
    a, b = t - timedelta(hours=6), t + timedelta(hours=6)
    try:
        rows = runtime.lake.query(
            "SELECT epoch_us(ts), value, attrs FROM observations WHERE type = 'fire' AND id = ? "
            "AND ts >= ? AND ts <= ? ORDER BY version DESC LIMIT 1", [subj["id"], a, b],
            start=a, end=b)
    except Exception:  # no partitions
        log.debug("fire detection query failed", exc_info=True)
        rows = []
    if rows:
        us, frp, attrs = rows[0]
        at = json.loads(attrs or "{}")
        out["detection"] = {"ts": _ts(us / 1e6), "frp_mw": frp,
                            **{k: at.get(k) for k in ("satellite", "instrument", "confidence",
                                                      "daynight", "bright_ti4", "scan")}}
    return out


FUNCS = {"track": section_track, "fire": section_fire,
         "environment": section_environment, "overhead": section_overhead,
         "signals": section_signals, "exposure": section_exposure,
         "activity": section_activity, "country": section_country,
         "attention": section_attention}
