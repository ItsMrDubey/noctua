"""Moving-point fit of carrier-aircraft fixes (osint.naval.airfix).

1. Group fixes in space-time: two fixes are linked when a ship at <= ``max_speed_kts`` could
   have been under both (distance <= v_max * |dt| + 2 * pos_tol) within ``max_link_h``.
   Distinct operating areas (Med / Red Sea / Arabian Sea / WestPac) fall apart naturally.
2. Within a group, sequential RANSAC over constant-velocity models (every fix as a stationary
   seed, every pair whose implied speed is <= v_max as a moving seed); inliers are refined by
   weighted least squares on a local tangent plane, speed clamped to v_max; the consensus
   set is removed and the rest is fitted again (two ships in one region stay separate).
3. Uncertainty: residual covariance (floored by the fix spread) / effective number of
   independent aircraft at the mean time, velocity covariance from the time lever arm (or an
   isotropic "unknown speed" prior when the time span is too short), plus manoeuvre growth
   ``drift_kmh`` per hour since the last fix -> a 2-D ellipse at "now" (95 %).

The output is a hypothesis about where aircraft were meeting a moving ship - never a ship
position."""

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from osint.core.geo import haversine_km
from osint.naval.airfix import AirFix
from osint.naval.geo import mean_point
from osint.naval.models import Evidence

KM_PER_KT_H = 1.852


class Plane:
    """Local equirectangular projection (km) around a reference point (antimeridian safe)."""

    def __init__(self, lat0: float, lon0: float) -> None:
        self.lat0, self.lon0 = lat0, lon0
        self.kx = 111.32 * max(0.05, math.cos(math.radians(lat0)))
        self.ky = 110.57

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        dlon = (lon - self.lon0 + 540.0) % 360.0 - 180.0
        return dlon * self.kx, (lat - self.lat0) * self.ky

    def ll(self, x: float, y: float) -> tuple[float, float]:
        lon = (self.lon0 + x / self.kx + 540.0) % 360.0 - 180.0
        return max(-89.9, min(89.9, self.lat0 + y / self.ky)), lon


@dataclass
class TrackFit:
    id: str
    fixes: list[AirFix]
    t_ref: datetime
    x0: float
    y0: float
    vx: float                  # km/h
    vy: float
    velocity_known: bool
    cov_p: list[list[float]]   # km^2 at t_ref
    cov_v: list[list[float]]   # (km/h)^2
    plane: Plane
    rms_km: float
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ derived

    @property
    def t_first(self) -> datetime:
        return min(f.ts for f in self.fixes)

    @property
    def t_last(self) -> datetime:
        return max(f.ts for f in self.fixes)

    @property
    def aircraft(self) -> list[str]:
        return sorted({f.track_id for f in self.fixes})

    @property
    def types(self) -> list[str]:
        return sorted({f.type_code for f in self.fixes if f.type_code})

    @property
    def speed_kts(self) -> float:
        return math.hypot(self.vx, self.vy) / KM_PER_KT_H

    @property
    def heading_deg(self) -> float:
        return (math.degrees(math.atan2(self.vx, self.vy)) + 360.0) % 360.0

    def at(self, t: datetime) -> tuple[float, float]:
        dt = (t - self.t_ref).total_seconds() / 3600.0
        return self.plane.ll(self.x0 + self.vx * dt, self.y0 + self.vy * dt)

    def cov_at(self, t: datetime, drift_kmh: float) -> list[list[float]]:
        dt = (t - self.t_ref).total_seconds() / 3600.0
        since = max(0.0, (t - self.t_last).total_seconds() / 3600.0)
        g = (drift_kmh * since) ** 2
        p, v = self.cov_p, self.cov_v
        return [[p[0][0] + dt * dt * v[0][0] + g, p[0][1] + dt * dt * v[0][1]],
                [p[1][0] + dt * dt * v[1][0], p[1][1] + dt * dt * v[1][1] + g]]

    def ellipse(self, t: datetime, cfg: dict) -> dict:
        c = self.cov_at(t, float(cfg.get("drift_kmh", 10)))
        a, b, d = c[0][0], c[0][1], c[1][1]
        tr, det = a + d, a * d - b * b
        disc = math.sqrt(max(0.0, tr * tr / 4 - det))
        l1, l2 = tr / 2 + disc, max(0.0, tr / 2 - disc)
        # eigenvector of l1 -> azimuth from north (x east, y north)
        ex, ey = (b, l1 - a) if abs(b) > 1e-9 else ((1.0, 0.0) if a >= d else (0.0, 1.0))
        az = (math.degrees(math.atan2(ex, ey)) + 180.0) % 180.0
        k = float(cfg.get("ellipse_k", 2.4477))
        cap = float(cfg.get("max_axis_km", 1500))
        return {"semi_major_km": round(min(cap, k * math.sqrt(l1)), 1),
                "semi_minor_km": round(min(cap, k * math.sqrt(l2)), 1),
                "orientation_deg": round(az, 1), "confidence": 0.95}

    def to_dict(self, now: datetime, cfg: dict,
                is_ocean: Callable[[float, float], bool] | None = None) -> dict:
        proj, notes = self.projected(now, is_ocean)
        last = self.at(self.t_last)
        ell = self.ellipse(now, cfg)
        ell.update(lat=round(proj[0], 4), lon=round(proj[1], 4), at=now.isoformat())
        path = [[round(lon, 4), round(lat, 4), t.isoformat()]
                for t, (lat, lon) in ((t, self.at(t)) for t in (self.t_first, self.t_last))]
        if proj != last:
            path.append([round(proj[1], 4), round(proj[0], 4), now.isoformat()])
        return {
            "id": self.id, "n_fixes": len(self.fixes), "aircraft": self.aircraft,
            "types": self.types, "roles": sorted({f.role for f in self.fixes}),
            "t_first": self.t_first.isoformat(), "t_last": self.t_last.isoformat(),
            "velocity_known": self.velocity_known,
            "speed_kts": round(self.speed_kts, 1) if self.velocity_known else None,
            "heading_deg": round(self.heading_deg, 0) if self.velocity_known else None,
            "rms_km": round(self.rms_km, 1),
            "last": {"lat": round(last[0], 4), "lon": round(last[1], 4),
                     "at": self.t_last.isoformat()},
            "projected": {"lat": round(proj[0], 4), "lon": round(proj[1], 4),
                          "at": now.isoformat()},
            "ellipse": ell, "path": path, "notes": self.notes + notes,
            "fixes": [f.to_dict() for f in sorted(self.fixes, key=lambda f: f.ts)],
        }

    def projected(self, now: datetime, is_ocean: Callable[[float, float], bool] | None
                  ) -> tuple[tuple[float, float], list[str]]:
        p = self.at(now)
        if is_ocean is not None and self.velocity_known and not is_ocean(*p):
            return self.at(self.t_last), ["projection would cross land: held at the last fit"]
        return p, []


# ----------------------------------------------------------------------- fitting

def _groups(fixes: list[AirFix], vmax_kmh: float, tol: float, max_link_h: float
            ) -> list[list[AirFix]]:
    n = len(fixes)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order = sorted(range(n), key=lambda i: fixes[i].ts)
    for a_i, i in enumerate(order):
        fi = fixes[i]
        for j in order[a_i + 1:]:
            fj = fixes[j]
            dt = (fj.ts - fi.ts).total_seconds() / 3600.0
            if dt > max_link_h:
                break
            if haversine_km(fi.lat, fi.lon, fj.lat, fj.lon) <= vmax_kmh * dt + 2 * tol:
                parent[find(i)] = find(j)
    comps: dict[int, list[AirFix]] = {}
    for i in range(n):
        comps.setdefault(find(i), []).append(fixes[i])
    return list(comps.values())


def _residual(x, y, t, m) -> float:
    x0, y0, vx, vy, t0 = m
    return math.hypot(x - (x0 + vx * (t - t0)), y - (y0 + vy * (t - t0)))


def _ransac(pts: list[tuple[float, float, float, float]], vmax: float, inl: float,
            min_span: float) -> tuple[tuple, list[int]]:
    """pts: (x, y, t_h, weight). Returns (model, inlier indices)."""
    seeds = [(x, y, 0.0, 0.0, t) for x, y, t, _ in pts]
    for i, (xi, yi, ti, _) in enumerate(pts):
        for xj, yj, tj, _ in pts[i + 1:]:
            dt = tj - ti
            if abs(dt) < min_span / 2:
                continue
            vx, vy = (xj - xi) / dt, (yj - yi) / dt
            if math.hypot(vx, vy) <= vmax:
                seeds.append((xi, yi, vx, vy, ti))
    best, best_key = seeds[0], (-1.0, 0.0)
    best_in: list[int] = []
    for m in seeds:
        ins = [k for k, (x, y, t, _) in enumerate(pts) if _residual(x, y, t, m) <= inl]
        score = sum(pts[k][3] for k in ins)
        res = sum(_residual(*pts[k][:3], m) for k in ins)
        key = (round(score, 6), -res)
        if key > best_key:
            best, best_key, best_in = m, key, ins
    return best, best_in


def _refine(pts, ins, vmax: float, min_span: float):
    w = [pts[k][3] for k in ins]
    sw = sum(w)
    tb = sum(pts[k][2] * pts[k][3] for k in ins) / sw
    xb = sum(pts[k][0] * pts[k][3] for k in ins) / sw
    yb = sum(pts[k][1] * pts[k][3] for k in ins) / sw
    ts = [pts[k][2] for k in ins]
    stt = sum(pts[k][3] * (pts[k][2] - tb) ** 2 for k in ins)
    known = max(ts) - min(ts) >= min_span and stt > 1e-9
    vx = vy = 0.0
    if known:
        vx = sum(pts[k][3] * (pts[k][2] - tb) * (pts[k][0] - xb) for k in ins) / stt
        vy = sum(pts[k][3] * (pts[k][2] - tb) * (pts[k][1] - yb) for k in ins) / stt
        sp = math.hypot(vx, vy)
        if sp > vmax:
            vx, vy = vx * vmax / sp, vy * vmax / sp
    return (xb, yb, vx, vy, tb), known


def _fit_one(fixes: list[AirFix], cfg: dict) -> tuple["TrackFit", list[AirFix]]:
    vmax = float(cfg.get("max_speed_kts", 32)) * KM_PER_KT_H
    inl = float(cfg.get("inlier_km", 45))
    min_span = float(cfg.get("min_span_h", 1.0))
    lat0, lon0 = _centre(fixes)
    plane = Plane(lat0, lon0)
    t0 = min(f.ts for f in fixes)
    pts = [(*plane.xy(f.lat, f.lon), (f.ts - t0).total_seconds() / 3600.0, max(f.weight, 1e-3))
           for f in fixes]
    m, ins = _ransac(pts, vmax, inl, min_span)
    known = False
    for _ in range(2):  # refine, re-select inliers, refine again
        m, known = _refine(pts, ins, vmax, min_span)
        ins2 = [k for k, p in enumerate(pts) if _residual(p[0], p[1], p[2], m) <= inl]
        if not ins2 or set(ins2) == set(ins):
            break
        ins = ins2
    xb, yb, vx, vy, tb = m
    sw = sum(pts[k][3] for k in ins)
    sxx = syy = sxy = 0.0
    for k in ins:
        x, y, t, w = pts[k]
        rx, ry = x - (xb + vx * (t - tb)), y - (yb + vy * (t - tb))
        sxx, syy, sxy = sxx + w * rx * rx, syy + w * ry * ry, sxy + w * rx * ry
    sxx, syy, sxy = sxx / sw, syy / sw, sxy / sw
    inl_fixes = [fixes[k] for k in ins]
    fix_var = sum(f.sigma_km ** 2 for f in inl_fixes) / len(inl_fixes)
    floor = max(float(cfg.get("sigma_floor_km", 15)) ** 2, fix_var)
    s = [[max(sxx, 0.0) + floor, sxy], [sxy, max(syy, 0.0) + floor]]
    n_eff = max(1.0, float(len({f.track_id for f in inl_fixes}))
                + 0.25 * (len(inl_fixes) - len({f.track_id for f in inl_fixes})))
    cov_p = [[s[0][0] / n_eff, s[0][1] / n_eff], [s[1][0] / n_eff, s[1][1] / n_eff]]
    if known:
        stt = sum((pts[k][2] - tb) ** 2 for k in ins)
        cov_v = [[s[0][0] / stt, s[0][1] / stt], [s[1][0] / stt, s[1][1] / stt]]
    else:
        sv = (float(cfg.get("unknown_speed_kts", 16)) * KM_PER_KT_H) ** 2
        cov_v = [[sv, 0.0], [0.0, sv]]
    notes = []
    if not known:
        notes.append("velocity unknown (fixes span < "
                     f"{min_span:g} h): stationary model with a speed prior")
    first = min(inl_fixes, key=lambda f: f.ts)
    fid = "fit-" + hashlib.sha1(f"{first.track_id}|{first.ts.isoformat()}".encode()).hexdigest()[:10]
    fit = TrackFit(id=fid, fixes=inl_fixes, t_ref=t0 + timedelta(hours=tb), x0=xb, y0=yb,
                   vx=vx, vy=vy, velocity_known=known, cov_p=cov_p, cov_v=cov_v, plane=plane,
                   rms_km=math.sqrt(max(0.0, sxx + syy)), notes=notes)
    rest = [f for k, f in enumerate(fixes) if k not in set(ins)]
    return fit, rest


def _centre(fixes: list[AirFix]) -> tuple[float, float]:
    return mean_point([(f.lat, f.lon) for f in fixes])


def fit_tracks(fixes: list[AirFix], cfg: dict) -> list[TrackFit]:
    """All moving-point fits of a set of fixes (every fix ends up in exactly one fit)."""
    if not fixes:
        return []
    vmax = float(cfg.get("max_speed_kts", 32)) * KM_PER_KT_H
    groups = _groups(fixes, vmax, float(cfg.get("pos_tol_km", 40)),
                     float(cfg.get("max_link_h", 36)))
    out: list[TrackFit] = []
    for g in groups:
        rest = sorted(g, key=lambda f: f.ts)
        while rest:
            fit, rest = _fit_one(rest, cfg)
            out.append(fit)
    out.sort(key=lambda f: f.t_last, reverse=True)
    return out


def fit_evidence(fits: list[TrackFit], now: datetime, cfg: dict,
                 is_ocean: Callable[[float, float], bool] | None = None) -> list[Evidence]:
    """One 'air_track' evidence per fit with enough fixes, at the fit projected to ``now``
    with the ellipse's major semi-axis as its spread. Independence keys = the aircraft."""
    out = []
    min_fixes = int(cfg.get("min_fixes_evidence", 2))
    for f in fits:
        if len(f.fixes) < min_fixes:
            continue
        d = f.to_dict(now, cfg, is_ocean)
        n_ac = len(f.aircraft)
        q = min(1.0, max(x.weight for x in f.fixes) / 2.0)
        w = min(float(cfg.get("max_weight", 4.0)),
                float(cfg.get("weight", 1.5)) * (1 + 0.5 * (n_ac - 1)) * q)
        if len(f.fixes) == 1:
            w *= float(cfg.get("single_fix_factor", 0.5))
        e = d["ellipse"]
        mv = (f"~{d['speed_kts']:.0f} kt heading {d['heading_deg']:.0f} deg"
              if f.velocity_known else "speed unknown")
        roles = "/".join(d["roles"])
        out.append(Evidence(
            kind="air_track", ts=f.t_last, lat=d["projected"]["lat"], lon=d["projected"]["lon"],
            weight=w,
            description=(f"moving-point fit of {len(f.fixes)} {roles} fixes from {n_ac} aircraft "
                         f"({', '.join(f.types) or 'n/a'}), {mv}, last fix "
                         f"{f.t_last:%H:%MZ %d %b}; projected to now +/- "
                         f"{e['semi_major_km']:.0f} x {e['semi_minor_km']:.0f} km (95 %)"),
            refs=[{"type": "track", "id": t} for t in f.aircraft[:8]],
            radius_km=max(30.0, e["semi_major_km"]), source_key="fit:" + f.id,
            source_keys=list(f.aircraft), detail=d))
    return out
