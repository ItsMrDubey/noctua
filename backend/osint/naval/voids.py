"""Commercial traffic voids (Poisson drop vs a 14-day baseline per H3 res-4 ocean cell) and
diversions (transiting tracks that bend around a void)."""

import math
from collections import defaultdict
from datetime import datetime

import h3

from osint.core.geo import haversine_km
from osint.naval.geo import angle_diff, bearing_deg, boundary, centre, mean_point
from osint.naval.models import Evidence


def poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k) for X ~ Poisson(lam)."""
    if lam <= 0 or k >= lam * 4 + 50:
        return 1.0
    if k < 0:
        return 0.0
    ln = math.log(lam)
    # log-space terms so large lambdas do not underflow exp(-lam)
    return min(1.0, sum(math.exp(i * ln - lam - math.lgamma(i + 1)) for i in range(k + 1)))


def normal_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2))


def baseline_means(counts: dict[str, dict[int, int]], all_bins: list[int]
                   ) -> dict[str, tuple[float, int, float]]:
    """cell -> (mean distinct vessels per bin, bins observed, variance per bin). Bins where
    the whole feed reported nothing are excluded (``all_bins`` = bins with any data at all)."""
    out = {}
    n = len(all_bins)
    if not n:
        return out
    for c, per_bin in counts.items():
        vals = [per_bin.get(b, 0) for b in all_bins]
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / max(1, n - 1)
        out[c] = (mean, n, var)
    return out


def detect_voids(baseline: dict[str, tuple], recent: dict[int, dict[str, int]],
                 now_bin: int, cfg: dict, *, global_ratio: float,
                 is_ocean=lambda c: True) -> list[dict]:
    """Cells whose recent distinct-vessel count is significantly below baseline.

    ``recent`` = {bin: {cell: count}} for closed recent bins; tested over the last k bins for
    each k in cfg.recent_bins (6/12/24 h); the most significant k wins. Returns
    [{cell, expected, observed, p, ratio, hours}]."""
    if global_ratio < float(cfg.get("min_global_ratio", 0.5)):
        return []
    min_mean = float(cfg.get("min_baseline_mean", 3.0))
    min_bins = int(cfg.get("min_baseline_bins", 12))
    max_p = float(cfg.get("max_p", 0.01))
    max_ratio = float(cfg.get("max_ratio", 0.5))
    bin_h = float(cfg.get("bin_hours", 6))
    out = []
    for c, b_stats in baseline.items():
        mean, nb = b_stats[0], b_stats[1]
        var = b_stats[2] if len(b_stats) > 2 else mean
        if mean < min_mean or nb < min_bins or not is_ocean(c):
            continue
        best = None
        for k in cfg.get("recent_bins", [1, 2, 4]):
            bins = [now_bin - i for i in range(1, int(k) + 1)]
            if not all(b in recent for b in bins):
                continue
            obs = sum(recent[b].get(c, 0) for b in bins)
            exp = mean * len(bins) * global_ratio
            sd = math.sqrt(max(var, mean) * len(bins)) * global_ratio
            p = max(poisson_cdf(obs, exp), normal_cdf((obs - exp) / sd) if sd > 0 else 0.0)
            ratio = obs / exp if exp else 1.0
            if p < max_p and ratio < max_ratio and (best is None or p < best["p"]):
                best = {"cell": c, "expected": round(exp, 1), "observed": obs, "p": p,
                        "ratio": round(ratio, 3), "hours": len(bins) * bin_h}
        if best:
            out.append(best)
    return out


def merge_cells(cells: list[str]) -> list[list[str]]:
    """Connected components of adjacent cells."""
    left = set(cells)
    comps = []
    while left:
        start = left.pop()
        comp, todo = [start], [start]
        while todo:
            cur = todo.pop()
            for nb in h3.grid_ring(cur, 1):
                if nb in left:
                    left.remove(nb)
                    comp.append(nb)
                    todo.append(nb)
        comps.append(comp)
    return comps


def void_polygon(comp: list[str]) -> list[list[float]]:
    """Outer ring [[lon, lat]...] of a cell cluster (first polygon of the H3 shape)."""
    try:
        shape = h3.cells_to_h3shape(comp)
        poly = shape.polys[0] if hasattr(shape, "polys") else shape
        ring = [[round(lo, 3), round(la, 3)] for la, lo in poly.outer]
        return ring + ring[:1]
    except Exception:  # noqa: BLE001 - degenerate shapes: fall back to the first cell
        return boundary(comp[0])


def void_evidence(voids: list[dict], now: datetime, weight: float) -> list[Evidence]:
    by_cell = {v["cell"]: v for v in voids}
    out = []
    for comp in merge_cells(list(by_cell)):
        lat, lon = mean_point([centre(c) for c in comp])
        exp = sum(by_cell[c]["expected"] for c in comp)
        obs = sum(by_cell[c]["observed"] for c in comp)
        p = min(by_cell[c]["p"] for c in comp)
        hours = max(by_cell[c]["hours"] for c in comp)
        radius = max(haversine_km(lat, lon, *centre(c)) for c in comp) + 20.0
        out.append(Evidence(
            kind="traffic_void", ts=now, lat=lat, lon=lon,
            weight=weight * min(2.0, 0.7 + 0.3 * len(comp)),
            description=(f"commercial traffic void: {obs} vessels vs ~{exp:.0f} expected over "
                         f"{hours:.0f} h in {len(comp)} cell(s) (Poisson p={p:.1e})"),
            refs=[{"type": "cell", "id": c} for c in comp[:20]],
            radius_km=radius, polygon=void_polygon(comp), source_key="void:" + comp[0]))
    return out


def diversion_evidence(voids: list[Evidence], tracks: dict[str, list[tuple]], cfg: dict,
                       now: datetime) -> list[Evidence]:
    """For each void: fraction of transiting tracks near it whose course bends > N degrees
    while their closest approach stays outside the void ("going around")."""
    bend = float(cfg.get("diversion_bend_deg", 25))
    min_n = int(cfg.get("diversion_min_tracks", 3))
    min_f = float(cfg.get("diversion_min_fraction", 0.4))
    w = float(cfg.get("diversion_weight", 0.8))
    out = []
    for v in voids:
        near_r = v.radius_km + 150.0
        n = bent = 0
        ids = []
        for tid, pts in tracks.items():
            if len(pts) < 3:
                continue
            dists = [haversine_km(v.lat, v.lon, p[1], p[2]) for p in pts]
            closest = min(dists)
            if closest > near_r:
                continue
            a, m, b = pts[0], pts[len(pts) // 2], pts[-1]
            if haversine_km(a[1], a[2], b[1], b[2]) < 30:  # not transiting
                continue
            n += 1
            d = abs(angle_diff(bearing_deg(a[1], a[2], m[1], m[2]),
                               bearing_deg(m[1], m[2], b[1], b[2])))
            if d > bend and closest > v.radius_km * 0.5:
                bent += 1
                ids.append(tid)
        if n >= min_n and bent / n >= min_f:
            out.append(Evidence(
                kind="traffic_diversion", ts=now, lat=v.lat, lon=v.lon, weight=w * bent / n,
                description=f"{bent}/{n} transiting vessels bent >{bend:.0f} deg around the void",
                refs=[{"type": "track", "id": t} for t in ids[:10]],
                radius_km=v.radius_km, source_key="div:" + v.source_key))
    return out


def bin_of(ts_s: float, bin_h: float) -> int:
    return int(ts_s // (bin_h * 3600))


def cells_res4(counts_by_h3_5: list[tuple[int, int, int]]) -> dict[int, dict[str, int]]:
    """[(h3_5 int, bin, distinct tracks)] -> {bin: {res4 cell: summed count}}."""
    out: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    parents: dict[int, str] = {}
    for c5, b, n in counts_by_h3_5:
        if c5 is None:
            continue
        p = parents.get(c5)
        if p is None:
            p = parents[c5] = h3.cell_to_parent(h3.int_to_str(c5), 4)
        out[b][p] += n
    return out
