"""Evidence fusion: time-decayed kernel-density surface on H3 res-4 ocean cells, clustering of
high-density cells into carrier-group hypotheses, heuristic confidence, evaluation."""

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

import h3

from osint.core.geo import haversine_km
from osint.naval.geo import RING_STEP_KM, OceanMask, boundary, cell, centre, mean_point
from osint.naval.models import Evidence

NAMING_KINDS = ("news", "usni_prior")
MAX_K = 25


def decay(ev: Evidence, now: datetime, half_life_h: float) -> float:
    hl = ev.half_life_h or half_life_h
    age_h = max(0.0, (now - ev.ts).total_seconds() / 3600.0)
    return ev.weight * 0.5 ** (age_h / hl)


@dataclass
class Surface:
    density: dict[str, float] = field(default_factory=dict)
    contrib: list[dict[str, float]] = field(default_factory=list)  # per evidence: cell -> value
    weights: list[float] = field(default_factory=list)              # decayed weight per evidence

    @property
    def peak(self) -> float:
        return max(self.density.values(), default=0.0)

    def cells_out(self, min_p: float = 0.02) -> list[dict]:
        peak = self.peak
        if peak <= 0:
            return []
        out = []
        for c, d in self.density.items():
            p = d / peak
            if p >= min_p:
                out.append({"h3": c, "boundary": boundary(c), "p": round(p, 4),
                            "density": round(d, 4)})
        out.sort(key=lambda x: -x["p"])
        return out


def build_surface(evidence: list[Evidence], now: datetime, cfg: dict, mask: OceanMask) -> Surface:
    """Gaussian kernel per evidence (sigma = max(bandwidth, radius/2)) over ocean cells.

    Peak amplitude = decayed weight * min(1, (bandwidth/sigma)^2): diffuse signals (wide
    warning areas, news, priors) are spread thin and cannot form hot cells on their own."""
    bw = float(cfg.get("bandwidth_km", 75))
    hl = float(cfg.get("half_life_h", 12))
    s = Surface()
    for ev in evidence:
        w = decay(ev, now, hl)
        s.weights.append(w)
        cmap: dict[str, float] = {}
        s.contrib.append(cmap)
        if w < 1e-4:
            continue
        sigma = max(bw, ev.radius_km / 2.0)
        amp = w * min(1.0, (bw / sigma) ** 2)
        k = min(MAX_K, math.ceil(2.5 * sigma / RING_STEP_KM))
        c0 = cell(ev.lat, ev.lon)
        for c in h3.grid_disk(c0, k):
            if mask.is_land_cell(c):
                continue
            la, lo = centre(c)
            d = haversine_km(ev.lat, ev.lon, la, lo)
            g = amp * math.exp(-(d * d) / (2 * sigma * sigma))
            if g < 1e-5:
                continue
            cmap[c] = g
            s.density[c] = s.density.get(c, 0.0) + g
    return s


def _components(hot: set[str], eps_km: float) -> list[list[str]]:
    k = max(1, math.ceil(eps_km / RING_STEP_KM))
    left = set(hot)
    comps = []
    while left:
        start = left.pop()
        comp, todo = [start], [start]
        while todo:
            cur = todo.pop()
            la, lo = centre(cur)
            for nb in h3.grid_disk(cur, k):
                if nb in left and haversine_km(la, lo, *centre(nb)) <= eps_km:
                    left.remove(nb)
                    comp.append(nb)
                    todo.append(nb)
        comps.append(comp)
    return comps


def kind_strengths(evidence: list[Evidence], shares: dict[int, float], weights: list[float],
                   cfg: dict) -> dict[str, float]:
    """Saturating strength per kind. One source (same kind + source_key) counts once (max)."""
    per_source: dict[tuple[str, str], float] = defaultdict(float)
    for i, share in shares.items():
        ev = evidence[i]
        key = (ev.kind, ev.source_key or f"#{i}")
        per_source[key] = max(per_source[key], weights[i] * share)
    total: dict[str, float] = defaultdict(float)
    for (kind, _), w in per_source.items():
        total[kind] += w
    scale = cfg.get("kind_scale") or {}
    strength = cfg.get("kind_strength") or {}
    return {k: float(strength.get(k, 0.3)) * (1 - math.exp(-w / float(scale.get(k, 2.0))))
            for k, w in total.items() if w > 0}


def independent_kinds(kinds: list[str], sources: dict[str, set[str]],
                      strengths: dict[str, float]) -> list[str]:
    """Kinds that add at least one source not already behind a stronger kind (one C-2 flight
    that is both 'cvw_aircraft' and 'sea_terminating_flight' is ONE independent signal)."""
    seen: set[str] = set()
    out = []
    for k in sorted(kinds, key=lambda k: -strengths.get(k, 0.0)):
        keys = sources.get(k) or {k}
        if keys - seen:
            out.append(k)
        seen |= keys
    return sorted(out)


def confidence(strengths: dict[str, float], cfg: dict,
               sources: dict[str, set[str]] | None = None) -> tuple[float, list[str], list[str]]:
    """1 - exp(-sum strengths); fewer than 2 independent positional kinds caps it.
    Returns (confidence, positional kinds present, independent positional kinds)."""
    positional = set(cfg.get("positional_kinds") or [])
    pos_kinds = sorted(k for k, v in strengths.items() if k in positional and v >= 0.05)
    indep = independent_kinds(pos_kinds, sources or {}, strengths)
    conf = 1 - math.exp(-sum(strengths.values()))
    if len(indep) < 2:
        conf = min(conf, float(cfg.get("single_kind_cap", 0.45)))
    return round(min(conf, float(cfg.get("max_confidence", 0.95))), 3), pos_kinds, indep


def hypotheses(evidence: list[Evidence], surface: Surface, cfg: dict, *,
               usni: dict | None = None, usni_cfg: dict | None = None) -> list[dict]:
    """Cluster hot cells into hypotheses (without ids; the job assigns stable ids)."""
    thr = float(cfg.get("min_cell_density", 0.25))
    hot = {c for c, d in surface.density.items() if d >= thr}
    if not hot:
        return []
    totals = [sum(m.values()) for m in surface.contrib]
    positional = set(cfg.get("positional_kinds") or [])
    out = []
    for comp in _components(hot, float(cfg.get("eps_km", 250))):
        cset = set(comp)
        ws = [surface.density[c] for c in comp]
        pts = [centre(c) for c in comp]
        lat, lon = mean_point(pts, ws)
        wsum = sum(ws)
        rms = math.sqrt(sum(w * haversine_km(lat, lon, *p) ** 2
                            for w, p in zip(ws, pts, strict=True)) / wsum)
        radius = max(float(cfg.get("min_radius_km", 50)), 1.5 * rms + 20.0)
        shares: dict[int, float] = {}
        contrib: dict[int, float] = {}
        for i, m in enumerate(surface.contrib):
            if not totals[i]:
                continue
            v = sum(val for c, val in m.items() if c in cset)
            if v > 0:
                contrib[i] = v
                shares[i] = v / totals[i]
        strengths = kind_strengths(evidence, shares, surface.weights, cfg)
        sources: dict[str, set[str]] = defaultdict(set)
        for i in shares:
            sources[evidence[i].kind].add(evidence[i].source_key or f"#{i}")
        conf, pos_kinds, indep = confidence(strengths, cfg, sources)
        if not pos_kinds:
            continue  # names/priors alone never make a hypothesis
        idx = sorted(contrib, key=lambda i: -contrib[i])
        top = idx[: int(cfg.get("max_evidence", 15))]
        evs = [{**evidence[i].to_dict(), "contribution": round(contrib[i], 4),
                "distance_km": round(haversine_km(lat, lon, evidence[i].lat, evidence[i].lon), 1)}
               for i in top]
        ts_all = [evidence[i].ts for i in idx if evidence[i].kind in positional]
        out.append({
            "lat": round(lat, 3), "lon": round(lon, 3), "radius_km": round(radius, 1),
            "confidence": conf, "kinds": pos_kinds, "independent_kinds": indep,
            "indicator_breakdown": {k: round(v, 3) for k, v in sorted(strengths.items())},
            "evidence_count": len(idx), "evidence": evs,
            "candidate_names": candidate_names(lat, lon, [evidence[i] for i in idx], usni,
                                               usni_cfg or {}),
            "signal_first": min(ts_all).isoformat() if ts_all else None,
            "signal_last": max(ts_all).isoformat() if ts_all else None,
            "peak_density": round(max(ws), 4), "cells": len(comp),
        })
    out.sort(key=lambda h: -h["confidence"])
    return out


def candidate_names(lat: float, lon: float, evs: list[Evidence], usni: dict | None,
                    usni_cfg: dict) -> list[dict]:
    names: dict[str, dict] = {}
    for ev in evs:
        if ev.kind != "news":
            continue
        for n in ev.names:
            d = haversine_km(lat, lon, ev.lat, ev.lon)
            cur = names.get(n)
            if cur is None or d < cur["distance_km"]:
                ref = ev.refs[0] if ev.refs else {}
                names[n] = {"name": n, "basis": "news mention (names, not positions)",
                            "source": ref.get("title") or ref.get("id"), "url": ref.get("url"),
                            "distance_km": round(d, 0)}
    max_km = float(usni_cfg.get("candidate_max_km", 1500))
    for e in (usni or {}).get("entries", []):
        if e.get("lat") is None or e.get("status") != "at_sea":
            continue
        d = haversine_km(lat, lon, e["lat"], e["lon"])
        if d > max_km:
            continue
        cur = names.get(e["carrier"])
        if cur is None or d < cur["distance_km"]:
            names[e["carrier"]] = {
                "name": e["carrier"],
                "basis": (f"USNI Fleet Tracker {usni.get('as_of')} prior: '{e.get('region')}' "
                          "(coarse region centroid)"),
                "source": "USNI News Fleet Tracker", "url": usni.get("url"),
                "distance_km": round(d, 0)}
    return sorted(names.values(), key=lambda x: x["distance_km"])


def _age_days(as_of: str | None) -> float | None:
    try:
        d = datetime.fromisoformat(as_of or "")
    except ValueError:
        return None
    d = d if d.tzinfo else d.replace(tzinfo=UTC)
    return round((datetime.now(UTC) - d).total_seconds() / 86400, 1)


def evaluate(usni: dict | None, hyps: list[dict], slack_km: float = 500.0) -> dict:
    """Distance from each USNI at-sea carrier's stated region to the nearest hypothesis."""
    if not usni or not usni.get("entries"):
        return {"available": False, "reason": "no USNI tracker parsed yet", "entries": []}
    rows = []
    at_sea = [e for e in usni["entries"] if e.get("status") == "at_sea" and e.get("lat") is not None]
    for e in usni["entries"]:
        row = {"carrier": e["carrier"], "hull": e.get("hull"), "region": e.get("region"),
               "status": e.get("status"), "region_lat": e.get("lat"), "region_lon": e.get("lon"),
               "nearest_hypothesis": None, "distance_km": None, "within_radius": False,
               "within_radius_plus_slack": False}
        if e.get("lat") is not None and hyps:
            best = min(hyps, key=lambda h: haversine_km(e["lat"], e["lon"], h["lat"], h["lon"]))
            d = haversine_km(e["lat"], e["lon"], best["lat"], best["lon"])
            row.update(nearest_hypothesis=best.get("id"), distance_km=round(d, 0),
                       hypothesis_confidence=best["confidence"],
                       within_radius=d <= best["radius_km"],
                       within_radius_plus_slack=d <= best["radius_km"] + slack_km)
        rows.append(row)
    hit = [r for r in rows if r["status"] == "at_sea" and r["within_radius_plus_slack"]]
    confident = [h for h in hyps if h["confidence"] > 0.5]
    supported = [h for h in confident if any(
        haversine_km(h["lat"], h["lon"], e["lat"], e["lon"]) <= h["radius_km"] + slack_km
        for e in at_sea)]
    return {
        "available": True, "as_of": usni.get("as_of"), "url": usni.get("url"),
        "via": usni.get("via", "category page"), "as_of_age_days": _age_days(usni.get("as_of")),
        "region_slack_km": slack_km,
        "at_sea_carriers": len(at_sea), "hypotheses": len(hyps),
        "confident_hypotheses": len(confident),
        "recall_within_radius": (sum(r["within_radius"] for r in rows if r["status"] == "at_sea")
                                 / len(at_sea)) if at_sea else None,
        "recall_with_slack": len(hit) / len(at_sea) if at_sea else None,
        "precision_confident_with_slack": (len(supported) / len(confident)) if confident else None,
        "notes": ("USNI covers US carriers only and states coarse regions (centroids from a "
                  "config table), so precision understates non-US activity and slack is "
                  "needed; hypotheses are evaluated from a fusion run WITHOUT the USNI prior."),
        "entries": rows,
    }
