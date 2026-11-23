"""NOAA SWPC space weather products -> one compact status snapshot, plus the OVATION aurora
nowcast as a 360 x 181 byte grid."""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime
from typing import Any

_HEAD_RE = re.compile(r"^(EXTENDED WARNING|WARNING|ALERT|WATCH|SUMMARY|CANCEL\w*|CONTINUED ALERT)"
                      r"\s*:\s*(.+)$", re.IGNORECASE)
_SCALE_RE = re.compile(r"NOAA Scale:\s*([GSR])(\d)", re.IGNORECASE)
# alerts at or above these scales are surfaced in the alert feed
ALERT_SCALES = {"G": 2, "S": 1, "R": 2}


def _ts(raw: Any) -> float | None:
    """SWPC time tags ('2026-11-19 01:18:16.670', '2026-11-23T04:15:00Z') -> unix seconds."""
    if not raw:
        return None
    s = str(raw).strip().replace(" ", "T").removesuffix("Z")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def parse_kp(payload: Any) -> list[list]:
    """Planetary K-index series -> [[unix s, kp], ...]. Accepts the list-of-dicts format and
    the older list-of-lists one (header row first)."""
    out = []
    for row in payload or []:
        if isinstance(row, dict):
            t, kp = _ts(row.get("time_tag")), row.get("Kp", row.get("kp_index"))
            if row.get("estimated_kp") is not None:
                kp = row["estimated_kp"]
        elif isinstance(row, list) and len(row) >= 2:
            t, kp = _ts(row[0]), row[1]
        else:
            continue
        try:
            kp = float(kp)
        except (TypeError, ValueError):
            continue
        if t is not None:
            out.append([t, round(kp, 2)])
    return out


def g_scale(kp: float) -> int:
    """NOAA G-scale for a Kp value (Kp 5 = G1 ... Kp 9 = G5)."""
    return max(0, min(5, int(kp + 1e-9) - 4))


def parse_scales(payload: Any) -> dict:
    """noaa-scales.json -> {"now": {G,S,R}, "today_max": ..., "forecast": [...]}."""
    def row(d: dict) -> dict:
        out = {}
        for k in ("G", "S", "R"):
            v = (d.get(k) or {}).get("Scale")
            out[k] = int(v) if str(v).isdigit() else None
        r = d.get("R") or {}
        out["r_minor_prob"], out["r_major_prob"] = r.get("MinorProb"), r.get("MajorProb")
        out["s_prob"] = (d.get("S") or {}).get("Prob")
        out["g_text"] = (d.get("G") or {}).get("Text")
        out["date"] = d.get("DateStamp")
        return out
    p = payload or {}
    return {"now": row(p.get("0") or {}), "today_max": row(p.get("-1") or {}),
            "forecast": [row(p[k]) for k in ("1", "2", "3") if k in p]}


def parse_alert(item: dict) -> dict | None:
    msg = str(item.get("message") or "")
    lines = [ln.strip() for ln in msg.splitlines() if ln.strip()]
    head = next((m for m in (_HEAD_RE.match(ln) for ln in lines) if m), None)
    issued = _ts(item.get("issue_datetime"))
    if issued is None:
        return None
    scales = {k.upper(): int(v) for k, v in _SCALE_RE.findall(msg)}
    kind = head.group(1).upper() if head else "MESSAGE"
    notable = kind in ("ALERT", "WARNING", "EXTENDED WARNING", "WATCH") and any(
        scales.get(k, 0) >= v for k, v in ALERT_SCALES.items())
    return {"id": f"{item.get('product_id')}:{int(issued)}", "product": item.get("product_id"),
            "issued": issued, "kind": kind,
            "headline": head.group(2).strip() if head else (lines[0] if lines else ""),
            "scales": scales, "notable": notable,
            "text": "\n".join(lines[:14])[:1200]}


def parse_alerts(payload: Any, limit: int = 30) -> list[dict]:
    out = [a for a in (parse_alert(x) for x in payload or [] if isinstance(x, dict)) if a]
    out.sort(key=lambda a: -a["issued"])
    return out[:limit]


def _first(payload: Any) -> dict:
    return payload[0] if isinstance(payload, list) and payload and isinstance(
        payload[0], dict) else {}


def solar_wind(speed: Any, mag: Any) -> dict:
    s, m = _first(speed), _first(mag)
    return {"speed_kms": s.get("proton_speed"), "bt_nt": m.get("bt"), "bz_nt": m.get("bz_gsm"),
            "at": _ts(s.get("time_tag") or m.get("time_tag"))}


def parse_flares(payload: Any) -> list[dict]:
    out = []
    for f in payload or []:
        if not isinstance(f, dict):
            continue
        out.append({"begin": _ts(f.get("begin_time")), "max": _ts(f.get("max_time")),
                    "end": _ts(f.get("end_time")), "class": f.get("max_class")})
    return out


def aurora_grid(payload: dict) -> dict | None:
    """OVATION nowcast (coordinates [lon 0..359, lat -90..90, probability %]) -> base64 of a
    360 x 181 uint8 grid, row 0 = lat -90, column 0 = lon 0."""
    coords = (payload or {}).get("coordinates")
    if not isinstance(coords, list):
        return None
    grid = bytearray(360 * 181)
    peak = 0
    for c in coords:
        try:
            lon, lat, v = int(c[0]) % 360, int(c[1]), int(c[2])
        except (TypeError, ValueError, IndexError):
            continue
        if -90 <= lat <= 90:
            v = max(0, min(100, v))
            grid[(lat + 90) * 360 + lon] = v
            peak = max(peak, v)
    return {"w": 360, "h": 181, "lon0": 0, "lat0": -90, "max": peak,
            "observed": payload.get("Observation Time"),
            "forecast": payload.get("Forecast Time"),
            "b64": base64.b64encode(bytes(grid)).decode()}
