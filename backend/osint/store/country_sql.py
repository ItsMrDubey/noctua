"""The country context rules (spec B §1) as SQL fragments over ``events``, plus the same
rules per event in Python (``event_ctx``) so the API never makes the UI recompute them.

For a selected country X (ISO 3166-1 alpha-2):

  location     inside        events.country = X
               abroad        events.country is not X (or NULL) and X is in actor_countries
  involvement  domestic      attributed and every known country (location + actors) = X
               foreign       some actor or location country != X
               unattributed  inside and not attributed (involvement unknown)

Unattributed events are never domestic. Historical (curated UCDP) rows are excluded."""

from __future__ import annotations

import json

LOCATIONS = ("all", "inside", "abroad")
INVOLVEMENTS = ("all", "domestic", "foreign", "unattributed")
# columns the country view adds to the slim list rows
COUNTRY_COLUMNS = ("actor_countries", "attributed", "lifecycle")
LIFECYCLE_RANK_SQL = ("CASE events.lifecycle WHEN 'developing' THEN 0 WHEN 'active' THEN 1 "
                      "WHEN 'cooling' THEN 2 WHEN 'closed' THEN 3 ELSE 4 END")

_ACTOR_IS = ("EXISTS (SELECT 1 FROM json_each(events.actor_countries) "
             "WHERE json_each.value = ?)")
_ACTOR_NOT = ("EXISTS (SELECT 1 FROM json_each(events.actor_countries) "
              "WHERE json_each.value != ?)")
_INSIDE = "events.country = ?"
_ABROAD = f"((events.country IS NULL OR events.country != ?) AND {_ACTOR_IS})"


def country_where(cc: str, location: str = "all", involvement: str = "all",
                  counterpart: str | None = None) -> tuple[list[str], list[object]]:
    """WHERE clauses (to AND together) and their parameters, in order."""
    where: list[str] = []
    params: list[object] = []
    if location == "inside":
        where.append(_INSIDE)
        params.append(cc)
    elif location == "abroad":
        where.append(_ABROAD)
        params += [cc, cc]
    else:
        where.append(f"({_INSIDE} OR {_ABROAD})")
        params += [cc, cc, cc]
    if involvement == "domestic":
        where.append("(events.attributed = 1 AND (events.country IS NULL OR events.country = ?)"
                     f" AND NOT {_ACTOR_NOT})")
        params += [cc, cc]
    elif involvement == "foreign":
        where.append(f"((events.country IS NOT NULL AND events.country != ?) OR {_ACTOR_NOT})")
        params += [cc, cc]
    elif involvement == "unattributed":
        where.append("(events.country = ? AND coalesce(events.attributed, 0) = 0)")
        params.append(cc)
    if counterpart:
        where.append(f"(events.country = ? OR {_ACTOR_IS})")
        params += [counterpart, counterpart]
    where.append("events.origin != 'historical'")
    return where, params


def as_list(value) -> list[str]:
    """actor_countries as a list of ISO2 strings, whether stored JSON text or decoded."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, str) and x]


def event_ctx(cc: str, country: str | None, actor_countries, attributed) -> dict:
    """``{location, involvement}`` of one event as seen from ``cc`` (same rules as the SQL)."""
    actors = as_list(actor_countries)
    location = "inside" if country == cc else "abroad"
    if location == "inside" and not attributed:
        involvement = "unattributed"
    elif (country is not None and country != cc) or any(a != cc for a in actors):
        involvement = "foreign"
    else:
        involvement = "domestic"
    return {"location": location, "involvement": involvement}
