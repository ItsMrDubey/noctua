"""GNSS interference from ADS-B navigation-integrity reports (the gpsjam.org method).

An ADS-B v1/v2 aircraft reports how good its own position is: NIC (integrity containment
radius category, 0..11) and NACp (accuracy category, 0..11). A GPS jammer drives both down
to 0; readsb also marks an aircraft whose GPS went bad (``gpsOkBefore``). Counting, per H3
cell and time window, the share of aircraft with degraded navigation gives an interference
map: below ``medium_pct`` (2 %) normal, up to ``high_pct`` (10 %) elevated, above it likely
jamming. Only direct ADS-B (``type == adsb_icao``) airborne reports are used: MLAT / TIS-B
positions are computed on the ground and carry no aircraft-reported integrity.

Spoofing (a false position reported with high integrity) is not detectable this way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import h3

DEFAULTS: dict[str, Any] = {
    "res": 3,  # H3 resolution of the map cells (~60 km edge; gpsjam aggregates per day)
    "window_min": 60,  # live map: aircraft seen in the last hour
    "nic_min": 7,  # NIC below this counts as degraded (7 = Rc < 0.2 NM)
    "nacp_min": 8,  # NACp below this counts as degraded (8 = EPU < 0.05 NM)
    "gps_lost_s": 600,  # gpsOkBefore within this many seconds = GPS currently lost
    "min_alt_ft": 1000,
    "max_age_s": 60,  # position older than this in the response is ignored
    "min_aircraft": 3,  # cells with fewer aircraft are not shown
    "medium_pct": 2.0,
    "high_pct": 10.0,
    "min_bad_high": 2,  # "high" needs at least this many degraded aircraft
}


@dataclass(frozen=True, slots=True)
class NavSample:
    hex: str
    lat: float
    lon: float
    ts: float  # unix seconds of the position
    bad: bool
    nic: int | None
    nac_p: int | None
    gps_lost: bool


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def nav_sample(ac: dict, now_s: float, cfg: dict | None = None) -> NavSample | None:
    """One readsb aircraft record -> its navigation-integrity sample, or None when it does
    not qualify (not direct ADS-B, on the ground / low, no position, no integrity fields)."""
    c = {**DEFAULTS, **(cfg or {})}
    if ac.get("type") != "adsb_icao":
        return None
    hex_code = str(ac.get("hex") or "").strip().lower()
    lat, lon = ac.get("lat"), ac.get("lon")
    if not hex_code or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    alt = ac.get("alt_baro")
    if alt == "ground" or (isinstance(alt, (int, float)) and alt < c["min_alt_ft"]):
        return None
    if _int(ac.get("version")) == 0:
        return None  # ADS-B v0 has no NACp and a different NIC meaning
    age = ac.get("seen_pos", ac.get("seen", 0))
    age = float(age) if isinstance(age, (int, float)) else 0.0
    if age > c["max_age_s"]:
        return None
    nic, nac_p = _int(ac.get("nic")), _int(ac.get("nac_p"))
    ok_before = ac.get("gpsOkBefore")
    gps_lost = isinstance(ok_before, (int, float)) and now_s - ok_before <= c["gps_lost_s"]
    if nic is None and nac_p is None and not gps_lost:
        return None
    bad = gps_lost or (nic is not None and nic < c["nic_min"]) or (
        nac_p is not None and nac_p < c["nacp_min"])
    return NavSample(hex_code, float(lat), float(lon), now_s - age, bool(bad), nic, nac_p,
                     bool(gps_lost))


def level(n: int, bad: int, cfg: dict | None = None) -> str:
    """'low' | 'medium' | 'high' for ``bad`` degraded out of ``n`` aircraft."""
    c = {**DEFAULTS, **(cfg or {})}
    pct = 100.0 * bad / n if n else 0.0
    if pct > c["high_pct"] and bad >= c["min_bad_high"]:
        return "high"
    if pct > c["medium_pct"] and bad >= 1:
        return "medium"
    return "low"


def cell_boundary(cell: str) -> list[float]:
    """Flat [lon, lat, lon, lat, ...] ring of an H3 cell (3 decimals)."""
    out: list[float] = []
    for lat, lon in h3.cell_to_boundary(cell):
        out += [round(lon, 3), round(lat, 3)]
    return out


class GnssAggregator:
    """Per-cell aircraft sets: a rolling live window and closed hourly buckets.

    An aircraft counts once per cell and period; it is degraded there if any of its samples in
    that cell were degraded."""

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.res = int(self.cfg["res"])
        # cell -> hex -> (last seen ts, degraded)
        self.live: dict[str, dict[str, tuple[float, bool]]] = {}
        # hour start (unix s) -> cell -> hex -> degraded
        self.hours: dict[int, dict[str, dict[str, bool]]] = {}
        self.samples = 0

    def add(self, samples: list[NavSample]) -> None:
        for s in samples:
            cell = h3.latlng_to_cell(s.lat, s.lon, self.res)
            per = self.live.setdefault(cell, {})
            prev = per.get(s.hex)
            per[s.hex] = (max(s.ts, prev[0]) if prev else s.ts, s.bad or (prev[1] if prev else False))
            hour = int(s.ts // 3600 * 3600)
            hb = self.hours.setdefault(hour, {}).setdefault(cell, {})
            hb[s.hex] = hb.get(s.hex, False) or s.bad
            self.samples += 1

    def prune(self, now_s: float) -> None:
        cutoff = now_s - float(self.cfg["window_min"]) * 60
        for cell in list(self.live):
            per = {h: v for h, v in self.live[cell].items() if v[0] >= cutoff}
            if per:
                self.live[cell] = per
            else:
                del self.live[cell]

    def snapshot(self, now_s: float) -> list[dict]:
        """Live cells with at least ``min_aircraft`` aircraft, worst first."""
        self.prune(now_s)
        out = []
        for cell, per in self.live.items():
            n = len(per)
            if n < int(self.cfg["min_aircraft"]):
                continue
            bad = sum(1 for _, b in per.values() if b)
            out.append(cell_item(cell, n, bad, self.cfg))
        out.sort(key=lambda c: (-c["pct"], -c["n"]))
        return out

    def pop_closed_hours(self, now_s: float) -> list[tuple[int, str, int, int]]:
        """(hour start, cell, aircraft, degraded) of every hourly bucket that has ended."""
        current = int(now_s // 3600 * 3600)
        out = []
        for hour in sorted(h for h in self.hours if h < current):
            for cell, per in self.hours.pop(hour).items():
                out.append((hour, cell, len(per), sum(1 for b in per.values() if b)))
        return out


def cell_item(cell: str, n: int, bad: int, cfg: dict | None = None) -> dict:
    lat, lon = h3.cell_to_latlng(cell)
    pct = round(100.0 * bad / n, 1) if n else 0.0
    return {"h3": cell, "lat": round(lat, 4), "lon": round(lon, 4), "n": n, "bad": bad,
            "pct": pct, "level": level(n, bad, cfg), "b": cell_boundary(cell)}
