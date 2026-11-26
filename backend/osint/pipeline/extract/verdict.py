"""Normalise an LLM extraction and compare it with the event it is meant to support.

verdict: ``unsupported`` (the article reports no concrete event), ``mismatch`` (the
article's event is somewhere else / of an incompatible type) or ``supported``.
"""

import re
import sqlite3
import zipfile
from collections.abc import Callable
from dataclasses import dataclass

from osint.core.countries import to_iso2
from osint.core.geo import haversine_km
from osint.enrich.coords import grounded_coordinate
from osint.pipeline.extract.schema import (
    CLAIM_TYPES,
    EVENT_TYPES,
    IMPACT_KINDS,
    IMPACT_SITES_MAX,
    PRECISIONS,
    QUOTE_MAX,
    ROLES,
    SCHEMA_VERSION,
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


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v)


def impact_sites(raw_sites, text: str, folded_text: str | None = None) -> list[dict]:
    """Grounded impact sites: a site is kept only when its quote is verbatim in the text;
    coordinates only when the same coordinates are written in the text (an LLM-supplied
    point that is not in the article is dropped, never used)."""
    folded_text = _fold(text) if folded_text is None else folded_text
    out: list[dict] = []
    seen: set[str] = set()
    for s in raw_sites or []:
        if not isinstance(s, dict):
            continue
        name, quote = _str(s.get("name"), 120), _str(s.get("quote"), QUOTE_MAX)
        if not name or not quote or _fold(quote) not in folded_text:
            continue  # ungrounded: the evidence span is not in the article
        key = _fold(name)
        if key in seen:
            continue
        seen.add(key)
        coords = grounded_coordinate(_num(s.get("lat")), _num(s.get("lon")), quote, text)
        out.append({"name": name,
                    "kind": s.get("kind") if s.get("kind") in IMPACT_KINDS else "other",
                    "context": _str(s.get("context"), 160),
                    "lat": coords[0] if coords else None,
                    "lon": coords[1] if coords else None,
                    "coords_stated": coords is not None, "quote": quote, "verbatim": True})
        if len(out) >= IMPACT_SITES_MAX:
            break
    return out


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
            # ISO2 per the instruction, checked against the registry; a non-ISO2 answer
            # such as "UK" resolves through the country names
            "country": to_iso2(country, "auto"),
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
        "impact_sites": impact_sites(raw.get("impact_sites"), text, folded_text),
        "schema_version": SCHEMA_VERSION,
    }


def effective_verdict(verdict: str | None, reasons: list[str] | None) -> str | None:
    """The verdict the event pipeline applies: a mismatch on the event *type* (the
    article reports an untracked ``other`` event, or an incompatible category) means the
    article does not support this event at all -> ``unsupported`` (capped confidence,
    hidden from the default map). Place/country mismatches stay ``mismatch``."""
    if verdict == "mismatch" and any(str(r).startswith("type:") for r in reasons or ()):
        return "unsupported"
    return verdict


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
            elif not ex_country and country and ev_country and country != ev_country:
                # no stated country (e.g. "Gulf of Finland"): fall back to the geocoded one
                reasons.append(f"country: article place {place} geocodes to {country}, "
                               f"event {ev_country}")
    return Verdict("mismatch" if reasons else "supported", reasons, distance, geo)


def gazetteer_geocoder(gazetteer, cities=None) -> Geocoder:
    """Geocoder backed by osint.enrich.gazetteer (LazyGazetteer or Gazetteer). With
    ``cities`` (``CountryCities``, GeoNames cities1000) a place in a stated country is first
    looked up there, so towns below the cities15000 cut-off resolve to city precision."""
    from osint.enrich.gazetteer import best_place

    def geocode(place: str, admin1: str, country: str):
        if cities is not None and country:
            try:
                hit = cities.lookup(place, admin1, country)
            except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile):  # never fatal
                hit = None
            if hit is not None:
                return hit.lat, hit.lon, hit.country, hit.precision
        text = ", ".join(x for x in (place, admin1) if x)
        hits = gazetteer.match(text, "en", country_hint=country)
        if country:
            hits = [h for h in hits if h.country == country] or []
        hit = best_place(hits)
        if hit is None:
            return None
        return hit.lat, hit.lon, hit.country, hit.precision

    return geocode
