"""Country context API (spec B §2.2): the per-country summary behind the country pane.

Counts, lifecycle, counterparts and top actors cover the non-historical events in the
country's location set (inside or abroad, osint.store.country_sql) updated in the window
(default: the last 7 days). ``spark`` always covers the last 30 days by first-report day.
``threat`` is the Terror Watch official level + advisories (``board.threat_section``), or
null when Terror Watch is unavailable. ``bbox`` comes from osint.core.countries; ``centroid``
(the Natural Earth label point) is the fly-to fallback for countries without a polygon."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

from fastapi import FastAPI
from fastapi.responses import Response

from osint.api import events as events_api
from osint.api.events import _json, clamp_since, country_code, parse_since, unknown_country
from osint.core import countries
from osint.signals.countries import Countries, get_countries
from osint.terror import board
from osint.terror.store import get_store as terror_store

log = logging.getLogger(__name__)

SPARK_DAYS = 30
DEFAULT_WINDOW = timedelta(days=7)
TOP_COUNTERPARTS = 15
TOP_ACTORS = 10
LIFECYCLES = ("developing", "active", "cooling", "closed")


def _shapes() -> Countries:
    """Natural Earth country label points and outlines (the IODA overlay's source)."""
    return get_countries()


def summarise(cc: str, rows: list[dict], spark_rows: list[tuple[str, int]],
              today: date) -> dict:
    """Counts, lifecycle, top actors, counterparts and the 30-day spark from
    ``SqliteStore.country_events`` rows and ``country_spark`` rows."""
    counts = dict.fromkeys(("inside", "abroad", "domestic", "foreign", "unattributed"), 0)
    lifecycle = dict.fromkeys(LIFECYCLES, 0)
    actors: Counter = Counter()
    n: Counter = Counter()
    as_location: Counter = Counter()
    as_actor: Counter = Counter()
    for r in rows:
        ctx = r["ctx"]
        counts[ctx["location"]] += 1
        counts[ctx["involvement"]] += 1
        if r.get("lifecycle") in lifecycle:
            lifecycle[r["lifecycle"]] += 1
        seen: set[tuple] = set()
        for a in r.get("actors") or []:
            if not isinstance(a, dict) or not str(a.get("name") or "").strip():
                continue
            key = (str(a["name"]).strip(), a.get("country"))
            if key not in seen:
                seen.add(key)
                actors[key] += 1
        if ctx["involvement"] != "foreign":
            continue
        loc, acs = r.get("country"), set(r.get("actor_countries") or [])
        for other in ({loc} | acs) - {cc, None}:
            n[other] += 1
            if other == loc:
                as_location[other] += 1
            if other in acs:
                as_actor[other] += 1
    by_day = dict(spark_rows)
    spark = []
    for i in range(SPARK_DAYS - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        spark.append({"day": day, "n": by_day.get(day, 0)})
    top = sorted(actors.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1] or ""))
    cps = sorted(n.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "counts": counts, "lifecycle": lifecycle, "spark": spark,
        "top_actors": [{"name": k[0], "country": k[1], "n": v} for k, v in top[:TOP_ACTORS]],
        "counterparts": [{"cc": c, "name": countries.name(c), "n": v,
                          "as_location": as_location[c], "as_actor": as_actor[c]}
                         for c, v in cps[:TOP_COUNTERPARTS]],
    }


def threat_for(runtime, cc: str) -> dict | None:
    """``board.threat_section`` for ``cc``, or None (logged) when Terror Watch fails."""
    try:
        return board.threat_section(terror_store(runtime), cc)
    except Exception:
        log.warning("threat level unavailable for %s", cc, exc_info=True)
        return None


def mount_country(app: FastAPI, runtime, *, now: Callable[[], datetime] | None = None) -> None:
    store = runtime.store
    clock = now or (lambda: events_api._utcnow())  # late-bound: tests pin events._utcnow

    @app.get("/api/country/{cc}/summary")
    async def country_summary(cc: str, since: str | None = None, until: str | None = None,
                              category: str | None = None) -> Response:
        """Header strip, filter counts and counterparts of the country pane."""
        code = country_code(cc)
        if code is None:
            return unknown_country()
        t = clock()
        hi = parse_since(until)
        lo = clamp_since(parse_since(since) or t - DEFAULT_WINDOW, hi or t)
        today = t.astimezone(UTC).date()
        spark_from = datetime.combine(today - timedelta(days=SPARK_DAYS - 1), time(0), UTC)
        rows = await asyncio.to_thread(store.country_events, code, since=lo, until=hi,
                                       category=category)
        spark = await asyncio.to_thread(store.country_spark, code, spark_from)
        threat = await asyncio.to_thread(threat_for, runtime, code)
        point = (await asyncio.to_thread(_shapes)).point(code)
        box = countries.bbox(code)
        return _json({"cc": code, "name": countries.name(code), "threat": threat,
                      **summarise(code, rows, spark, today),
                      "bbox": list(box) if box else None,
                      "centroid": [point[0], point[1]] if point else None,
                      "since": lo.isoformat(), "until": hi.isoformat() if hi else None})
