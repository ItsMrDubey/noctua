"""Normalise an LLM extraction and compare it with the event it is meant to support.

verdict: ``unsupported`` (the article reports no concrete event), ``mismatch`` (the
article's event is somewhere else / of an incompatible type) or ``supported``.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from osint.core.geo import haversine_km
from osint.pipeline.extract.schema import (
    CLAIM_TYPES,
    EVENT_TYPES,
    PRECISIONS,
    QUOTE_MAX,
    ROLES,
    SUMMARY_MAX_WORDS,
)

# event category -> extracted event types that can describe the same event
COMPATIBLE: dict[str, set[str]] = {
    "armed_conflict": {"armed_conflict", "air_activity", "maritime_security", "infrastructure",
                       "civil_unrest", "political"},
    "air_activity": {"air_activity", "armed_conflict", "maritime_security", "political",
                     "infrastructure"},
    "maritime_security": {"maritime_security", "armed_conflict", "air_activity", "disaster",
                          "economic", "infrastructure"},
    "civil_unrest": {"civil_unrest", "political", "armed_conflict"},
    "disaster": {"disaster", "infrastructure"},
    "infrastructure": {"infrastructure", "disaster", "armed_conflict", "cyber_outage",
                       "economic", "maritime_security"},
    "political": {"political", "economic", "civil_unrest", "armed_conflict", "cyber_outage",
                  "maritime_security", "air_activity"},
    "economic": {"economic", "political", "infrastructure", "maritime_security",
                 "cyber_outage"},
    "cyber_outage": {"cyber_outage", "infrastructure", "political", "economic"},
}
FINE_PRECISIONS = {"point", "city"}
_WS = re.compile(r"\s+")

# (place_name, admin1, country ISO2) -> (lat, lon, country ISO2, precision) or None
Geocoder = Callable[[str, str, str], tuple[float, float, str, str] | None]


@dataclass
class Verdict:
    verdict: str  # supported | unsupported | mismatch
    reasons: list[str]
    distance_km: float | None = None
    geo: dict | None = None  # geocoded extracted place {lat, lon, country, precision}


def _fold(s: str) -> str:
    return _WS.sub(" ", s).strip().casefold()


def _str(v, limit: int = 300) -> str | None:
    if v is None:
        return None
    s = _WS.sub(" ", str(v)).strip()
    return s[:limit] or None


def _int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return max(0, v)
    if isinstance(v, float):
        return max(0, int(v))
    return None


def normalise(raw: dict, text: str) -> dict:
    """Coerce an LLM result into the schema; quotes are capped and checked verbatim."""
    where = raw.get("where") if isinstance(raw.get("where"), dict) else {}
    country = (_str(where.get("country"), 8) or "").upper()
    cas = raw.get("casualties") if isinstance(raw.get("casualties"), dict) else {}
    words = (_str(raw.get("summary"), 600) or "").split()
    summary = " ".join(words[:SUMMARY_MAX_WORDS])
    folded_text = _fold(text)
    quotes = []
    for q in raw.get("quotes") or []:
        if not isinstance(q, dict) or not _str(q.get("quote")):
            continue
        quote = _str(q.get("quote"), QUOTE_MAX)
        quotes.append({"field": _str(q.get("field"), 40) or "other", "quote": quote,
                       "verbatim": _fold(quote) in folded_text})
    actors = []
    for a in raw.get("actors") or []:
        if isinstance(a, dict) and _str(a.get("name")):
            role = a.get("role") if a.get("role") in ROLES else "other"
            actors.append({"name": _str(a["name"], 120), "role": role,
                           "type": _str(a.get("type"), 60)})
    etype = raw.get("event_type")
    return {
        "is_real_event": bool(raw.get("is_real_event")),
        "event_type": etype if etype in EVENT_TYPES else "other",
        "subtype": _str(raw.get("subtype"), 80),
        "summary": summary,
        "when": _str(raw.get("when"), 40),
        "where": {
            "place_name": _str(where.get("place_name"), 120),
            "admin1": _str(where.get("admin1"), 120),
            "country": country if re.fullmatch(r"[A-Z]{2}", country) else None,
            "precision": where.get("precision") if where.get("precision") in PRECISIONS
            else "unknown",
        },
        "actors": actors[:20],
        "casualties": {"killed": _int(cas.get("killed")), "injured": _int(cas.get("injured"))},
        "weapons_platforms": [s for s in (_str(x, 80) for x in raw.get("weapons_platforms")
                                          or []) if s][:20],
        "cause_stated": _str(raw.get("cause_stated"), 300),
        "consequences": [s for s in (_str(x, 200) for x in raw.get("consequences") or [])
                         if s][:20],
        "claim_type": raw.get("claim_type") if raw.get("claim_type") in CLAIM_TYPES
        else "unverified",
        "quotes": quotes[:20],
    }


def judge(event: dict, ext: dict, *, geocode: Geocoder | None = None,
          mismatch_km: float = 300.0) -> Verdict:
    """Compare a normalised extraction with the event (columns from SqliteStore)."""
    if not ext.get("is_real_event"):
        return Verdict("unsupported", ["article does not report a concrete event"])
    reasons: list[str] = []
    category = event.get("category") or ""
    etype = ext.get("event_type") or "other"
    allowed = COMPATIBLE.get(category)
    if allowed is not None and etype not in allowed:
        reasons.append(f"type: article is {etype}, event is {category}")
    where = ext.get("where") or {}
    ev_country = (event.get("country") or "").upper()
    ex_country = where.get("country") or ""
    if ev_country and ex_country and ev_country != ex_country:
        reasons.append(f"country: article {ex_country}, event {ev_country}")
    distance = None
    geo = None
    place = where.get("place_name")
    if geocode is not None and place and where.get("precision") in ("point", "city", "admin1"):
        hit = geocode(place, where.get("admin1") or "", ex_country)
        if hit is not None:
            lat, lon, country, precision = hit
            geo = {"lat": lat, "lon": lon, "country": country, "precision": precision}
            if (precision in FINE_PRECISIONS and event.get("precision") in FINE_PRECISIONS
                    and event.get("lat") is not None and event.get("lon") is not None):
                distance = round(haversine_km(lat, lon, event["lat"], event["lon"]), 1)
                if distance > mismatch_km:
                    reasons.append(f"place: {place} is {distance:.0f} km from the event's "
                                   f"{event.get('place_name') or 'point'}")
    return Verdict("mismatch" if reasons else "supported", reasons, distance, geo)


def gazetteer_geocoder(gazetteer) -> Geocoder:
    """Geocoder backed by osint.enrich.gazetteer (LazyGazetteer or Gazetteer)."""
    from osint.enrich.gazetteer import best_place

    def geocode(place: str, admin1: str, country: str):
        text = ", ".join(x for x in (place, admin1) if x)
        hits = gazetteer.match(text, "en", country_hint=country)
        if country:
            hits = [h for h in hits if h.country == country] or []
        hit = best_place(hits)
        if hit is None:
            return None
        return hit.lat, hit.lon, hit.country, hit.precision

    return geocode
