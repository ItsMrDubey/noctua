"""The country context rules (spec B §1) as SQL fragments over ``events``, plus the same
rules per event in Python (``event_ctx``) so the API never makes the UI recompute them.

For a selected country X (ISO 3166-1 alpha-2):

  location     inside        events.country = X
               abroad        events.country is not X (or NULL) and X is in actor_countries
  involvement  domestic      attributed and every known country (location + actors) = X
               foreign       some actor or location country != X
               unattributed  not attributed, and inside (or location unknown with X the
                             only actor): involvement unknown, so never domestic

Unattributed events are never domestic. Historical (curated UCDP) rows are excluded."""

from __future__ import annotations

import json

LOCATIONS = ("all", "inside", "abroad")
INVOLVEMENTS = ("all", "domestic", "foreign", "unattributed")
# columns the country view adds to the slim list rows
COUNTRY_COLUMNS = ("actor_countries", "attributed", "lifecycle")
LIFECYCLE_RANK_SQL = ("CASE events.lifecycle WHEN 'developing' THEN 0 WHEN 'active' THEN 1 "
                      "WHEN 'cooling' THEN 2 WHEN 'closed' THEN 3 ELSE 4 END")

# actor_countries as as_list() reads it: only a JSON array counts (anything else is no actors),
# and only its non-empty text elements are countries
_ACTORS = ("(CASE WHEN json_valid(events.actor_countries) THEN CASE WHEN "
           "json_type(events.actor_countries) = 'array' THEN events.actor_countries END END)")
_ACTOR_IS = (f"EXISTS (SELECT 1 FROM json_each({_ACTORS}) "
             "WHERE json_each.type = 'text' AND json_each.value = ?)")
_ACTOR_NOT = (f"EXISTS (SELECT 1 FROM json_each({_ACTORS}) "
              "WHERE json_each.type = 'text' AND json_each.value != '' "
              "AND json_each.value != ?)")
_ATTRIBUTED = "coalesce(events.attributed, 0) != 0"
_INSIDE = "events.country = ?"
_ABROAD = f"((events.country IS NULL OR events.country != ?) AND {_ACTOR_IS})"
# event_ctx's first rule: inside and not attributed is unattributed, whatever the actors say
_INSIDE_UNATTR = f"(events.country IS ? AND NOT {_ATTRIBUTED})"


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
    # the involvement filters mirror event_ctx branch by branch (row in F <=> ctx(row) == F),
    # including rows whose columns disagree (inside, not attributed, foreign actors)
    if involvement == "domestic":
        where.append(f"({_ATTRIBUTED} AND (events.country IS NULL OR events.country = ?)"
                     f" AND NOT {_ACTOR_NOT})")
        params += [cc, cc]
    elif involvement == "foreign":
        where.append(f"(NOT {_INSIDE_UNATTR} AND ((events.country IS NOT NULL AND "
                     f"events.country != ?) OR {_ACTOR_NOT}))")
        params += [cc, cc, cc]
    elif involvement == "unattributed":
        # inside and not attributed, or location unknown, X the only actor, not attributed
        where.append(f"(NOT {_ATTRIBUTED} AND (events.country = ? OR "
                     f"(events.country IS NULL AND NOT {_ACTOR_NOT})))")
        params += [cc, cc]
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
    elif not attributed:
        involvement = "unattributed"  # never domestic (location unknown, X the only actor)
    else:
        involvement = "domestic"
    return {"location": location, "involvement": involvement}
