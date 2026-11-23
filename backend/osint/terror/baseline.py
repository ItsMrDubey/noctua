"""Historical base rates from UCDP one-sided violence (type 3) by non-state actors.

UCDP GED (final, yearly) plus the UCDP Candidate monthly releases, CC BY 4.0. Aggregated
to (month, country, actor) counts of events and deaths (best / low / high). Rows from the
government side ("Government of ...") are dropped: state violence is outside the working
definition. Each actor is mapped to a Terror Watch group when the KB lists it
(``ucdp_actors``); the terrorism base-rate proxy uses only mapped, non-criminal groups.

Caveat shown with every number: UCDP one-sided violence counts deliberate killings of
civilians by an organised actor that kills at least 25 civilians in a year. It excludes
non-lethal attacks, attacks on security forces (coded as state-based conflict) and most
lone-actor attacks -- it is a proxy for terrorist lethality, not a terrorism count.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterable, Iterator

from osint.terror import countries

LICENCE = ("UCDP Georeferenced Event Dataset (GED) Global version and UCDP Candidate Events "
           "Dataset, Uppsala Conflict Data Program, CC BY 4.0")
CAVEAT = ("UCDP one-sided violence by non-state actors: deliberate killings of civilians by "
          "organised actors (>= 25 civilian deaths per actor-year). Excludes non-lethal "
          "attacks, attacks on security forces and most lone actors. A proxy for terrorist "
          "lethality, not a count of terrorist attacks.")
_GED_VER = re.compile(r"ged(\d+)", re.IGNORECASE)


def rows_from_zip(data: bytes) -> Iterator[dict]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        with z.open(name) as f:
            yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))


def rows_from_csv(data: bytes) -> Iterator[dict]:
    yield from csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def aggregate(sources: Iterable[tuple[str, Iterable[dict]]], kb=None) -> list[tuple]:
    """sources: [(dataset label, rows)] in priority order (final GED first, then candidate
    releases oldest -> newest). Each UCDP event id is counted once: a later candidate
    release replaces an earlier one, and a final GED row wins over any candidate row."""
    by_id: dict[str, tuple[str, dict]] = {}
    final_ids: set[str] = set()
    for label, rows in sources:
        # final yearly GED ("ged261-csv") vs Candidate releases ("GEDEvent_v26_0_10")
        final = label.lower().startswith("ged") and not label.lower().startswith("gedevent")
        for r in rows:
            if (r.get("type_of_violence") or "").strip() != "3":
                continue
            side_a = (r.get("side_a") or "").strip()
            if not side_a or side_a.startswith("Government of"):
                continue
            rid = (r.get("id") or "").strip()
            if not rid or (rid in final_ids and not final):
                continue
            if final:
                final_ids.add(rid)
            by_id[rid] = (label, r)
    agg: dict[tuple[str, str, str], list] = {}
    for label, r in by_id.values():
        month = (r.get("date_start") or "")[:7]
        cc = countries.iso2(r.get("country"))
        if not month or not cc:
            continue
        actor = r["side_a"].strip()
        key = (month, cc, actor)
        a = agg.setdefault(key, [label, 0, 0, 0, 0])
        a[1] += 1
        a[2] += _int(r.get("best"))
        a[3] += _int(r.get("low"))
        a[4] += _int(r.get("high"))
    out = []
    for (month, cc, actor), (label, n, best, low, high) in sorted(agg.items()):
        gid = kb.for_ucdp_actor(actor) if kb is not None else None
        cat = kb.get(gid).category if (kb is not None and gid and kb.get(gid)) else None
        out.append((month, cc, actor, gid, cat, label, n, best, low, high))
    return out


def monthly(rows: list[dict], *, designated_only: bool = True) -> dict[str, dict]:
    """{month: {"events", "deaths"}} summed over the given ucdp_osv rows."""
    out: dict[str, dict] = {}
    for r in rows:
        if designated_only and (not r.get("group_id") or r.get("category") == "criminal"):
            continue
        m = out.setdefault(r["month"], {"events": 0, "deaths": 0})
        m["events"] += r["events"]
        m["deaths"] += r["deaths_best"]
    return out


def annual(rows: list[dict], *, designated_only: bool = True) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for m, v in monthly(rows, designated_only=designated_only).items():
        y = out.setdefault(m[:4], {"events": 0, "deaths": 0, "months": 0})
        y["events"] += v["events"]
        y["deaths"] += v["deaths"]
        y["months"] += 1
    return out
