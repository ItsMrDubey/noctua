"""Internet outages from IODA (Georgia Tech Internet Outage Detection and Analysis).

IODA's ``outages/events`` endpoint lists per-country outage events per datasource with a
score. The datasources differ a lot in reliability for "the internet is down here":

* ``bgp`` (routed address space disappears) and ``ping-slash24`` (active probing) are strong.
* ``gtr`` (Google Transparency Report traffic) is good but slower.
* ``merit-nt`` (network telescope, unsolicited traffic) fires often on noise.

``weights`` scales each datasource's confidence; a country's severity is the best weighted,
log-scaled score of its events, and only events reaching ``event_min_weight`` (default:
not merit-nt alone) become platform events.
"""

from __future__ import annotations

import math
from typing import Any

DEFAULT_WEIGHTS = {"bgp": 1.0, "ping-slash24": 1.0, "gtr": 0.75, "merit-nt": 0.3}
DEFAULTS: dict[str, Any] = {
    "min_score": 100.0,  # events below this IODA score are ignored
    "event_min_weight": 0.7,
    "event_min_score": 1000.0,
    "ongoing_s": 1800,  # an event whose "until" is this close to now is ongoing
}


def severity(score: float, weight: float) -> float:
    """0..1: log10(score) mapped 2 -> 0, 6 -> 1, times the datasource weight."""
    if score <= 0:
        return 0.0
    return round(max(0.0, min(1.0, (math.log10(score) - 2.0) / 4.0)) * weight, 3)


def level(sev: float) -> str:
    return "high" if sev >= 0.6 else "medium" if sev >= 0.35 else "low"


def parse_events(payload: dict, now_s: float, cfg: dict | None = None,
                 weights: dict[str, float] | None = None) -> list[dict]:
    """IODA ``outages/events`` body -> event dicts (country code upper-case)."""
    c = {**DEFAULTS, **(cfg or {})}
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    out = []
    for e in (payload or {}).get("data") or []:
        if not isinstance(e, dict):
            continue
        ent = e.get("entity") or {}
        cc = str(ent.get("code") or "").upper()
        try:
            start, until, score = int(e["from"]), int(e["until"]), float(e.get("score") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if not cc or score < float(c["min_score"]):
            continue
        ds = str(e.get("datasource") or "")
        weight = float(w.get(ds, 0.5))
        out.append({
            "cc": cc, "name": ent.get("name") or cc, "datasource": ds,
            "method": e.get("method"), "from": start, "until": until, "score": round(score, 1),
            "weight": weight, "severity": severity(score, weight),
            "ongoing": now_s - until <= float(c["ongoing_s"]),
        })
    return out


def by_country(events: list[dict]) -> list[dict]:
    """Per-country roll-up: worst severity, its level, ongoing flag, datasources, span."""
    out: dict[str, dict] = {}
    for e in events:
        c = out.setdefault(e["cc"], {"cc": e["cc"], "name": e["name"], "severity": 0.0,
                                     "score": 0.0, "datasources": [], "ongoing": False,
                                     "from": e["from"], "until": e["until"], "events": 0})
        c["events"] += 1
        c["from"], c["until"] = min(c["from"], e["from"]), max(c["until"], e["until"])
        c["ongoing"] = c["ongoing"] or e["ongoing"]
        if e["datasource"] not in c["datasources"]:
            c["datasources"].append(e["datasource"])
        if e["severity"] > c["severity"]:
            c["severity"], c["score"] = e["severity"], e["score"]
    for c in out.values():
        c["level"] = level(c["severity"])
        c["datasources"].sort()
    return sorted(out.values(), key=lambda c: (-c["ongoing"], -c["severity"]))


def notable(e: dict, cfg: dict | None = None) -> bool:
    """True if an outage event is strong enough to become a platform event."""
    c = {**DEFAULTS, **(cfg or {})}
    return e["weight"] >= float(c["event_min_weight"]) and e["score"] >= float(
        c["event_min_score"])
