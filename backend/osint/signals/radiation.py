"""Ambient gamma dose rate (µSv/h) from public monitoring networks, and anomaly rules.

* BfS ODL (Germany's federal network, ~1,700 probes, hourly means; open data, dl-de/by-2-0)
  via the BfS GeoServer WFS layer ``opendata:odlinfo_odl_1h_latest``.
* Safecast realtime devices (citizen network, CC0) from ``tt.safecast.org/devices``; counts
  per minute are converted with the tube's factor (LND 7317/7318 pancake: 334 CPM per µSv/h,
  LND 7128 / 712: 108).

A station reading is ``elevated`` when it is at least ``ratio`` x its own median over the
baseline window and ``min_delta`` above it (rain washout of radon progeny can double a
reading for an hour or two, so the defaults ask for more than that), and above ``floor``;
an elevated reading at or above ``alert`` is an ``alert``. Absolute levels alone never flag a
station: some sit in contaminated areas (Safecast devices in the Chornobyl and Fukushima
zones read 1-3 µSv/h every day), so a station without ``min_samples`` of history is
``unknown`` for alerting; ``assess_detail`` still rates its reading against the short
history it has (``provisional``) so the UI can show normal / elevated instead of nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

CPM_PER_USVH = {"lnd_7318c": 334.0, "lnd_7318u": 334.0, "lnd_7128ec": 108.0, "lnd_712u": 108.0}
DEFAULTS: dict[str, Any] = {
    "ratio": 2.2, "min_delta": 0.1, "floor": 0.25, "alert": 1.0,
    "min_samples": 12, "max_age_h": 6,
}


def _ts(raw: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if dt.year < 2000:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_bfs(payload: dict) -> list[dict]:
    """BfS WFS GeoJSON -> readings of operating probes."""
    out = []
    for f in (payload or {}).get("features") or []:
        p = f.get("properties") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or []
        ts = _ts(p.get("end_measure"))
        value = p.get("value")
        if (len(coords) < 2 or ts is None or not isinstance(value, (int, float))
                or p.get("site_status") not in (1, None) or not p.get("id")):
            continue
        out.append({"id": f"bfs:{p['id']}", "source": "bfs", "name": p.get("name") or p["id"],
                    "country": "DE", "lat": float(coords[1]), "lon": float(coords[0]),
                    "ts": ts, "value": round(float(value), 4),
                    "cosmic": p.get("value_cosmic"), "unit": "µSv/h"})
    return out


def parse_safecast(payload: Any, now: datetime, max_age_h: float) -> list[dict]:
    """Safecast realtime device list -> recent readings converted to µSv/h."""
    out = []
    for d in payload or []:
        if not isinstance(d, dict):
            continue
        ts = _ts(d.get("when_captured"))
        lat, lon = d.get("loc_lat"), d.get("loc_lon")
        if ts is None or (now - ts).total_seconds() > max_age_h * 3600 or ts > now:
            continue
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        tube = next((k for k in CPM_PER_USVH if isinstance(d.get(k), (int, float))
                     and d[k] > 0), None)
        if tube is None or not d.get("device"):
            continue
        out.append({"id": f"safecast:{d['device']}", "source": "safecast",
                    "name": d.get("loc_name") or str(d.get("device_sn") or d["device"]),
                    "country": d.get("loc_country") or "", "lat": float(lat),
                    "lon": float(lon), "ts": ts,
                    "value": round(float(d[tube]) / CPM_PER_USVH[tube], 4),
                    "cpm": d[tube], "tube": tube, "unit": "µSv/h"})
    return out


def assess(value: float, baseline: list | tuple | None, cfg: dict | None = None) -> str:
    """'alert' | 'elevated' | 'normal' | 'unknown' (no usable baseline)."""
    c = {**DEFAULTS, **(cfg or {})}
    if not baseline or baseline[1] < int(c["min_samples"]):
        return "unknown"
    median = float(baseline[0])
    if (value >= float(c["ratio"]) * median and value >= median + float(c["min_delta"])
            and value >= float(c["floor"])):
        return "alert" if value >= float(c["alert"]) else "elevated"
    return "normal"


def assess_detail(value: float, baseline: list | tuple | None,
                  cfg: dict | None = None) -> tuple[str, bool]:
    """(status, provisional): like ``assess``, but a station with some history (fewer than
    ``min_samples`` readings) is rated against it and flagged provisional; 'unknown' only
    without any history."""
    c = {**DEFAULTS, **(cfg or {})}
    if not baseline or not baseline[1]:
        return "unknown", False
    if baseline[1] >= int(c["min_samples"]):
        return assess(value, baseline, c), False
    return assess(value, [baseline[0], int(c["min_samples"])], c), True
