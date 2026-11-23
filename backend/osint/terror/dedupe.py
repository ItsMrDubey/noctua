"""Incident-level duplicate merging (lesson 6: one attack reported in seven languages is
one incident).

The clusterer already merges cross-language articles into one event using multilingual
embeddings; this pass catches what it left split -- two events describing the same
incident. Two incidents merge only when ALL hold:

* same class, same country (or one unknown and the other within the distance limit);
* event times within ``max_hours`` (occurrence time when stated, else first report);
* locations within ``max_km`` when both are located, or the same folded place name;
* and at least one content signal: embedding cosine >= ``min_cosine`` (default 0.60; the events' stored
  multilingual vectors), the same linked group, or the same killed figure (> 0) with the
  same attack type.

The survivor is the incident with more independent outlets; the other is kept with
``merged_into`` (never deleted) and its documents count towards the survivor's outlets.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from osint.core.geo import haversine_km
from osint.terror.countries import fold


def _t(i: dict) -> datetime | None:
    s = i.get("occurred_at") or i.get("first_reported_at")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _killed(i: dict) -> int | None:
    k = ((i.get("casualties") or {}).get("killed") or {})
    return k.get("high")


def same_incident(a: dict, b: dict, vec_a, vec_b, *, max_hours: float = 48, max_km: float = 50,
                  min_cosine: float = 0.60) -> tuple[bool, str]:
    if a.get("klass") != b.get("klass") or a.get("klass") is None:
        return False, "class differs"
    if a.get("country") and b.get("country") and a["country"] != b["country"]:
        return False, "country differs"
    ta, tb = _t(a), _t(b)
    if ta is None or tb is None or abs((ta - tb).total_seconds()) > max_hours * 3600:
        return False, "time differs"
    if a.get("lat") is not None and b.get("lat") is not None:
        d = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
        if d > max_km:
            return False, f"{d:.0f} km apart"
        where = f"{d:.0f} km apart"
    elif a.get("place_name") and b.get("place_name") and \
            fold(a["place_name"]) == fold(b["place_name"]):
        where = "same place name"
    else:
        return False, "location not comparable"
    # contradicting details veto a merge (two attacks in one city on one day stay two)
    ta_, tb_ = a.get("attack_type"), b.get("attack_type")
    if ta_ and tb_ and "unknown" not in (ta_, tb_) and ta_ != tb_:
        return False, f"attack types differ ({ta_} / {tb_})"
    ka, kb = _killed(a), _killed(b)
    if ka and kb and max(ka, kb) > 2 * min(ka, kb) + 2:
        return False, f"death tolls differ ({ka} / {kb})"
    signals = []
    if vec_a is not None and vec_b is not None:
        cos = float(np.dot(vec_a, vec_b))
        if cos >= min_cosine:
            signals.append(f"text similarity {cos:.2f}")
    ga, gb = (a.get("group") or {}), (b.get("group") or {})
    if ga.get("kind") == "link" and ga.get("group_id") and ga.get("group_id") == gb.get("group_id"):
        signals.append(f"same group ({ga['group_id']})")
    if ka and ka == kb and a.get("attack_type") == b.get("attack_type"):
        signals.append(f"same killed figure ({ka}) and attack type")
    if not signals:
        return False, "no content signal"
    return True, f"{where}; " + "; ".join(signals)


def merge(incidents: list[dict], vectors: dict[str, np.ndarray] | None = None, **kw
          ) -> list[tuple[str, str, str]]:
    """[(merged id, survivor id, basis)] -- pairs to merge (transitively resolved)."""
    vectors = vectors or {}
    live = [i for i in incidents if i.get("status") in ("confirmed", "reported", "possible")]
    live.sort(key=lambda i: (-(i.get("outlets") or {}).get("n", 0), i["id"]))
    survivor: dict[str, str] = {}
    out = []
    for idx, a in enumerate(live):
        if a["id"] in survivor:
            continue
        members = [a]  # b joins when it matches the survivor or any report already merged
        for b in live[idx + 1:]:
            if b["id"] in survivor:
                continue
            for m in members:
                ok, why = same_incident(m, b, vectors.get(m["event_id"]),
                                        vectors.get(b["event_id"]), **kw)
                if ok:
                    survivor[b["id"]] = a["id"]
                    out.append((b["id"], a["id"], why))
                    members.append(b)
                    break
    return out
