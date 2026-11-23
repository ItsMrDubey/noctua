"""CelesTrak GP (OMM) element sets, compacted for client-side SGP4 propagation (satellite.js
``json2satrec``). OMM rather than TLE: the 5-digit TLE catalogue field runs out above 99999."""

from __future__ import annotations

from typing import Any

# compact row layout served to the client (rebuilt into an OMM object there)
FIELDS = ("norad", "name", "intl", "epoch", "mm", "ecc", "inc", "raan", "argp", "ma",
          "bstar", "mmdot", "mmddot")
_ELEMENTS = ("MEAN_MOTION", "ECCENTRICITY", "INCLINATION", "RA_OF_ASC_NODE",
             "ARG_OF_PERICENTER", "MEAN_ANOMALY")
_DRAG = ("BSTAR", "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT")  # absent = 0


def compact(omm: dict) -> list | None:
    """One CelesTrak OMM JSON record -> compact row, or None if it cannot be propagated."""
    try:
        norad = int(omm["NORAD_CAT_ID"])
        epoch = str(omm["EPOCH"])
        elements = [float(omm[k]) for k in _ELEMENTS]
        drag = [float(omm.get(k) or 0.0) for k in _DRAG]
    except (KeyError, TypeError, ValueError):
        return None
    name = str(omm.get("OBJECT_NAME") or f"NORAD {norad}").strip()
    return [norad, name, str(omm.get("OBJECT_ID") or ""), epoch, *elements, *drag]


def parse_group(payload: Any) -> list[list]:
    """CelesTrak ``FORMAT=json`` body -> compact rows (non-list bodies: empty)."""
    if not isinstance(payload, list):
        return []
    return [r for r in (compact(o) for o in payload if isinstance(o, dict)) if r is not None]


def derive(rows: list[list], prefixes: dict[str, str]) -> dict[str, list[int]]:
    """Group membership by object-name prefix (e.g. starlink: STARLINK) over ``rows``."""
    out: dict[str, list[int]] = {g: [] for g in prefixes}
    for r in rows:
        name = str(r[1]).upper()
        for g, p in prefixes.items():
            if name.startswith(str(p).upper()):
                out[g].append(r[0])
    return out


def select(cache: dict | None, groups: list[str], limit: int) -> dict:
    """Rows of the union of ``groups`` from the cached snapshot, each with its group names."""
    if not cache:
        return {"fields": [*FIELDS, "groups"], "items": [], "groups": {}, "truncated": False}
    objects: dict[str, list] = cache.get("objects") or {}
    member: dict[str, list[int]] = cache.get("membership") or {}
    want = [g for g in groups if g in member]
    tags: dict[int, list[str]] = {}
    for g in want:
        for n in member[g]:
            tags.setdefault(n, []).append(g)
    # every group an object belongs to (not just the requested ones) for the inspector
    all_tags: dict[int, list[str]] = {}
    for g, ids in member.items():
        if g == "active":
            continue
        for n in ids:
            if n in tags:
                all_tags.setdefault(n, []).append(g)
    items = []
    for n in sorted(tags):
        row = objects.get(str(n))
        if row is not None:
            items.append([*row, all_tags.get(n, tags[n])])
    truncated = len(items) > limit
    return {"fields": [*FIELDS, "groups"], "items": items[:limit], "truncated": truncated,
            "groups": {g: (cache.get("groups") or {}).get(g, {}) for g in member},
            "updated": cache.get("updated")}
