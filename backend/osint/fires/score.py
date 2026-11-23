"""Conflict likelihood of FIRMS hotspots: features from grid-indexed context, combined into
a 0-100 score plus reason codes (weights: config/fire_score.yaml).

Points add up from ``base``:

- conflict context (capped at ``context_cap``): distance to the Ukraine front line
  (full points within ``frontline_full_km``, linear to 0 at ``frontline_km``), UCDP GED
  event density within ``ucdp_km`` (log scale, full at ``ucdp_saturation``), located
  conflict news in the last ``news_hours`` within ``news_km`` (any precision),
  ``news_city_km`` (city-precision events sit on the city centre) or ``news_wide_km``;
- pattern points (night, isolated, urban, infrastructure, high FRP), scaled by
  ``gate = gate_min + (1 - gate_min) * min(1, context / gate_full)`` so a night-time
  town fire far from any conflict stays low;
- penalties, always applied: large contiguous clusters and burn regions (wildfire, crop
  burning), low confidence, static sites (recurring ~450 m cell, EOG gas flare, volcano).
  A static site burning far above its usual FRP (``static_anomaly``) is not penalised.

Reason codes are ordered by the size of their contribution; ``features`` keeps the raw
values (distances, counts, names) and the per-reason points for the inspector.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from osint.fires.config import FireScoreConfig
from osint.fires.geo import KM_PER_DEG, PointIndex, SegmentIndex


@dataclass(slots=True)
class Hotspot:
    id: str
    ts_us: int
    lat: float
    lon: float
    frp: float | None = None
    daynight: str = ""
    confidence: str = ""


class Recurrence:
    """Past-day bitmask (bit i = seen i days before the reference day) and median FRP per
    ~cell_deg cell; a hotspot's history is the union over its 3x3 neighbourhood (VIIRS
    geolocation jitter moves a fixed source across cell edges)."""

    def __init__(self, cells: dict[tuple[int, int], tuple[int, float | None]],
                 cell_deg: float, history_days: int) -> None:
        self.cells, self.deg, self.history_days = cells, cell_deg, history_days

    def key(self, lat: float, lon: float) -> tuple[int, int]:
        return math.floor(lat / self.deg), math.floor(lon / self.deg)

    def lookup(self, lat: float, lon: float) -> tuple[int, float | None]:
        """(distinct past days seen, max median FRP of the neighbourhood)."""
        cy, cx = self.key(lat, lon)
        mask, med = 0, None
        for y in (cy - 1, cy, cy + 1):
            for x in (cx - 1, cx, cx + 1):
                hit = self.cells.get((y, x))
                if hit is not None:
                    mask |= hit[0]
                    if hit[1] is not None and (med is None or hit[1] > med):
                        med = hit[1]
        return mask.bit_count(), med


class CountGrid:
    """Point counts per cell; ``approx`` sums the cells whose centre lies within a radius
    (O(cells), for regional densities where an exact count would scan thousands)."""

    def __init__(self, cell_deg: float = 0.1) -> None:
        self.deg = cell_deg
        self.counts: Counter[tuple[int, int]] = Counter()
        self._memo: dict[tuple[tuple[int, int], float], int] = {}

    def add(self, lat: float, lon: float) -> None:
        self.counts[(math.floor(lat / self.deg), math.floor(lon / self.deg))] += 1
        self._memo.clear()

    def approx(self, lat: float, lon: float, km: float) -> int:
        cy, cx = math.floor(lat / self.deg), math.floor(lon / self.deg)
        memo = self._memo.get(((cy, cx), km))
        if memo is not None:
            return memo
        clat = (cy + 0.5) * self.deg
        kx = KM_PER_DEG * max(0.01, math.cos(math.radians(clat)))
        ry = math.ceil(km / KM_PER_DEG / self.deg)
        rx = math.ceil(km / kx / self.deg)
        n = 0
        for y in range(cy - ry, cy + ry + 1):
            for x in range(cx - rx, cx + rx + 1):
                c = self.counts.get((y, x))
                if c and math.hypot((x - cx) * self.deg * kx,
                                    (y - cy) * self.deg * KM_PER_DEG) <= km:
                    n += c
        self._memo[((cy, cx), km)] = n
        return n


@dataclass
class Context:
    frontline: SegmentIndex | None = None
    ucdp: PointIndex | None = None          # payload unused
    news: PointIndex | None = None          # payload (event id, title, precision)
    cities: PointIndex | None = None        # payload (name, radius_km)
    pois: PointIndex | None = None          # payload (name, group)
    volcanoes: PointIndex | None = None     # payload name
    flares: PointIndex | None = None        # payload type
    recurrence: Recurrence | None = None
    meta: dict = field(default_factory=dict)


class Pool:
    """Hotspots of the current window, for the cluster signals. Isolation and cluster size
    count distinct ~``site_deg`` cells, not detections: one site seen by four sensors on
    two overpasses is still one site. Regional density counts detections."""

    def __init__(self, hotspots: list[Hotspot], site_deg: float = 0.004) -> None:
        sites: dict[tuple[int, int], tuple[float, float]] = {}
        self.region = CountGrid(0.1)
        for h in hotspots:
            sites.setdefault((math.floor(h.lat / site_deg), math.floor(h.lon / site_deg)),
                             (h.lat, h.lon))
            self.region.add(h.lat, h.lon)
        self.sites = PointIndex(((la, lo, None) for la, lo in sites.values()), cell_deg=0.02)


def score_one(h: Hotspot, pool: Pool, ctx: Context, cfg: FireScoreConfig) -> dict:
    c, p, w = cfg.context, cfg.pattern, cfg.w
    pts: dict[str, float] = {}
    info: list[str] = []
    feats: dict[str, object] = {}

    # ---- conflict context
    if ctx.frontline is not None and len(ctx.frontline):
        d = ctx.frontline.distance(h.lat, h.lon, c.frontline_km)
        if d is not None:
            feats["frontline_km"] = round(d, 1)
            span = max(1e-6, c.frontline_km - c.frontline_full_km)
            f = 1.0 if d <= c.frontline_full_km else max(0.0, (c.frontline_km - d) / span)
            if d <= c.near_frontline_km:
                pts["near_frontline"] = w("frontline") * f
            else:
                pts["frontline_zone"] = w("frontline") * f
    if ctx.ucdp is not None and len(ctx.ucdp):
        n = ctx.ucdp.count(h.lat, h.lon, c.ucdp_km)
        if n:
            feats["ucdp_n"] = n
            v = w("ucdp_hist") * min(1.0, math.log1p(n) / math.log1p(max(1, c.ucdp_saturation)))
            if n >= c.ucdp_min_events:
                pts["ucdp_hist"] = v
            else:
                pts["ucdp_weak"] = v
    if ctx.news is not None and len(ctx.news):
        hits = ctx.news.within(h.lat, h.lon, c.news_wide_km)
        if hits:
            code, (near_d, (eid, title, _)) = "conflict_news_25km", hits[0]
            if near_d <= c.news_km:
                code = "conflict_news_5km"
            else:
                city = next((x for x in hits if x[1][2] == "city"
                             and x[0] <= c.news_city_km), None)
                if city is not None:
                    code, (near_d, (eid, title, _)) = "conflict_news_city", city
            feats["news_n"] = len(hits)
            feats["news_km"] = round(near_d, 1)
            feats["news_event"] = {"id": eid, "title": title}
            pts[code] = w(code)
    context = min(w("context_cap"), sum(pts.values()))
    if sum(pts.values()) > context:  # scale the context reasons down to the cap
        k = context / sum(pts.values())
        pts = {r: v * k for r, v in pts.items()}
    gate = w("gate_min") + (1.0 - w("gate_min")) * min(1.0, context / max(1e-6, w("gate_full")))
    feats["context"] = round(context, 1)

    # ---- pattern
    positive: list[str] = []
    if h.daynight == "N":
        positive.append("night")
    n_iso = pool.sites.count(h.lat, h.lon, p.isolated_km) - 1
    n_clu = pool.sites.count(h.lat, h.lon, p.cluster_km) - 1
    n_reg = pool.region.approx(h.lat, h.lon, p.region_km) - 1
    feats.update(sites_2km=max(0, n_iso), cluster_n=max(0, n_clu), region_n=max(0, n_reg))
    if n_iso <= p.isolated_max:
        positive.append("isolated")
    if n_clu >= p.cluster_min:
        pts["large_cluster"] = w("large_cluster")
    if n_reg >= p.region_min:
        pts["burn_region"] = w("burn_region")
    if ctx.cities is not None and len(ctx.cities):
        for d, (name, radius) in ctx.cities.within(h.lat, h.lon, 25.0):
            if d <= radius:
                positive.append("urban")
                feats["place"] = name
                feats["place_km"] = round(d, 1)
                break
    if ctx.pois is not None and len(ctx.pois):
        hit = ctx.pois.nearest(h.lat, h.lon, p.poi_km)
        if hit is not None:
            positive.append("infrastructure")
            feats["facility"] = f"{hit[1][0]} ({hit[1][1].replace('_', ' ')})"
            feats["facility_km"] = round(hit[0], 1)
    if h.frp is not None and h.frp >= p.high_frp:
        positive.append("high_frp")
    if h.confidence.casefold().startswith("low"):
        pts["low_conf"] = w("low_conf")

    # ---- static sources
    static: list[str] = []
    median = None
    if ctx.volcanoes is not None and len(ctx.volcanoes):
        hit = ctx.volcanoes.nearest(h.lat, h.lon, p.volcano_km)
        if hit is not None:
            static.append("volcano")
            feats["volcano"] = hit[1]
    if ctx.flares is not None and len(ctx.flares):
        hit = ctx.flares.nearest(h.lat, h.lon, p.flare_km)
        if hit is not None:
            static.append("gas_flare")
            feats["flare_type"] = hit[1]
    rec = ctx.recurrence
    hc = cfg.history
    if rec is not None and rec.history_days >= hc.min_history_days:
        days, median = rec.lookup(h.lat, h.lon)
        feats["static_days"] = days
        feats["history_days"] = rec.history_days
        if days >= max(hc.min_days, hc.min_fraction * rec.history_days):
            static.append("static_site")
    if static:
        if median is not None:
            feats["median_frp"] = round(median, 1)
        floor = max(hc.anomaly_min_frp, hc.anomaly_factor * (median or 0.0))
        if "volcano" not in static and h.frp is not None and h.frp >= floor:
            positive.append("static_anomaly")
            info += static
        else:
            for r in static:
                pts[r] = w(r)

    for r in positive:
        pts[r] = w(r) * gate
    raw = w("base") + sum(pts.values())
    score = round(min(100.0, max(0.0, raw)))
    reasons = [r for r, v in sorted(pts.items(), key=lambda kv: -abs(kv[1])) if abs(v) >= 0.5]
    feats["points"] = {r: round(v, 1) for r, v in pts.items() if abs(v) >= 0.05}
    if info:
        feats["static_overridden"] = info
    return {"id": h.id, "score": score, "reasons": reasons, "features": feats}


def score_hotspots(hotspots: list[Hotspot], pool: Pool, ctx: Context,
                   cfg: FireScoreConfig) -> list[dict]:
    return [score_one(h, pool, ctx, cfg) for h in hotspots]
