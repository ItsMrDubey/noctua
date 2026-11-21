"""GDELT 2.0 benchmark scorecard: eight metrics over a rolling window (spec §1).

Inputs are read once per computation (``collect``) into small per-event rows and
aggregates — GDELT volume and geo statistics are aggregated inside DuckDB — then each
metric is a pure function of those inputs (``score``), which is what the tests exercise.

Every metric carries a plain-English ``definition`` so the numbers can be read without
this file. Where GDELT has no equivalent capability (article-level verification, sensor
corroboration) that is stated as a capability difference, never as a measured number.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise

import pyarrow as pa

from osint.benchmark.config import BenchmarkConfig
from osint.benchmark.data import Sources, parse_ts
from osint.benchmark.store import BenchmarkStore, iso, retention_cutoff
from osint.core.geo import utcnow

log = logging.getLogger(__name__)

PRECISIONS = ("point", "city", "admin1", "country", "unlocated")
PREC_RANK = {"unlocated": 0, "country": 1, "admin1": 2, "city": 3, "point": 4}
# GDELT ActionGeo_Type -> name / our precision scale
GDELT_GEO_TYPES = {"1": "country", "2": "us_state", "3": "us_city", "4": "world_city",
                   "5": "world_state"}
GDELT_TYPE_PRECISION = {"1": "country", "2": "admin1", "3": "city", "4": "city",
                        "5": "admin1"}
GDELT_CITY_TYPES = ("3", "4")
REJECT_REASONS = ("irrelevant_category", "stale", "no_location", "no_article_backing",
                  "country_centroid_only", "low_mentions")
# rejection reasons for rows in our tracked categories, fresh enough, that we did not
# attach: the honest "GDELT-only" coverage (irrelevant/stale rows are out of scope)
GDELT_ONLY_RELEVANT = ("no_article_backing", "no_location", "country_centroid_only",
                       "low_mentions")

DEFINITIONS = {
    "duplication": (
        "How many records describe one real-world incident. For GDELT: the GDELT rows our "
        "clusterer attached to the same event of ours (each attached row is a GDELT "
        "duplicate of an incident we merged into one event). For us: events per incident, "
        "estimated from possible residual duplicates (same category, same ~8 km H3 cell, "
        "start times close together); confirmed by the labelled gold set. Lower is better."),
    "geolocation": (
        "How precisely each record is placed. Ours: the precision of our events (point / "
        "city / admin1 / country / unlocated). GDELT: its ActionGeo_Type for the same window "
        "(1 country, 2 US state, 3 US city, 4 world city, 5 world state). Headline: share "
        "at city-or-better. Also counts GDELT country-centroid rows we rejected and, for "
        "rows matched to our events, whether our location was finer than GDELT's."),
    "latency": (
        "How long after the first article was published each system had the event. Ours: "
        "event first_seen minus the earliest article published_at. GDELT: earliest attached "
        "row's DATEADDED minus the same published_at (GDELT publishes in 15-minute "
        "batches). Median and 90th percentile in minutes, and the share of matched "
        "incidents where our event existed before GDELT's first row."),
    "coverage": (
        "What each system has that the other lacks in the window. Ours-only: our news "
        "events with no GDELT row attached. Both: events GDELT also coded. GDELT-only: "
        "GDELT rows we rejected, by reason; the relevant subset is rows in our tracked "
        "categories that no article-backed event matched."),
    "verification": (
        "Share of our news events whose article text was checked by the extractor "
        "(supported / mismatch / unsupported), reported by 2+ independent outlets, or "
        "backed by sensor evidence (fires, quakes, aircraft, ships, AIS gaps, imagery). "
        "GDELT has no article-level verification or sensor corroboration by construction: "
        "a capability difference, not a measured number."),
    "noise_rejection": (
        "Share of the GDELT rows we adjudicated that we rejected, and why: "
        "irrelevant_category (CAMEO code outside the categories we track), stale (event "
        "or DATEADDED too old), no_location, no_article_backing (no matching "
        "article-backed event)."),
    "cost": (
        "Spend per verified event: extraction LLM cost in the window plus AlterLab spend "
        "(when a usage record exists), divided by our events that are article-supported or "
        "sensor-corroborated. GDELT data is free but yields no verified events."),
    "quality": (
        "Human-labelled gold set: share of sampled events that are real, correctly grouped "
        "events, and share whose location is correct (precision@location). Reported only "
        "once enough labels exist."),
}


# ------------------------------------------------------------------------- inputs


@dataclass
class EventRow:
    id: str
    category: str
    precision: str
    status: str
    source_diversity: int
    first_seen: datetime | None
    t_start: datetime | None
    title: str = ""
    place_name: str | None = None
    h3_5: str | None = None
    sensor_derived: bool = False
    corroborated: bool = False
    verdict: str | None = None
    articles: int = 0
    first_article: datetime | None = None
    gdelt_rows: int = 0
    first_gdelt: datetime | None = None


@dataclass
class Inputs:
    now: datetime
    start: datetime
    events: list[EventRow] = field(default_factory=list)
    live_since: datetime | None = None
    # in-window decisions: (decision, reason) -> n
    decisions: Counter = field(default_factory=Counter)
    # accepted rows of window events: (event_id, geo_type or None)
    accepted_geo: list[tuple[str, str | None]] = field(default_factory=list)
    # in-window rejected rows: (reason, geo_type or None) -> n
    rejected_geo: Counter = field(default_factory=Counter)
    # GDELT lake rows published in the window: {"all"|"tracked": {geo_type: n}}
    lake_types: dict[str, Counter] = field(
        default_factory=lambda: {"all": Counter(), "tracked": Counter()})
    lake_keys: dict[str, int] = field(default_factory=lambda: {"all": 0, "tracked": 0})
    lake_adjudicated: int = 0
    # distinct approximate incidents (date, CAMEO root, place) among in-window rejected
    # rows of the GDELT_ONLY_RELEVANT reasons
    gdelt_only_incidents: int | None = None
    llm_cost_window: float = 0.0
    llm_cost_total: float = 0.0
    llm_calls_window: int = 0
    alterlab_cost: float | None = None
    labels: dict = field(default_factory=dict)  # see label_summary


# ------------------------------------------------------------------------ helpers


def pct(n: float, d: float) -> float | None:
    return round(100.0 * n / d, 2) if d else None


def ratio(n: float, d: float, nd: int = 2) -> float | None:
    return round(n / d, nd) if d else None


def quantile(values: list[float], q: float) -> float | None:
    """Linear-interpolated quantile (q in [0, 1]); None for an empty list."""
    if not values:
        return None
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    v = s[lo] + (s[hi] - s[lo]) * (pos - lo)
    return round(v, 1)


def _minutes(a: datetime, b: datetime) -> float:
    return (a - b).total_seconds() / 60.0


def _news(inp: Inputs) -> list[EventRow]:
    return [e for e in inp.events if not e.sensor_derived]


def _corroborated(e: EventRow) -> bool:
    return e.corroborated or e.status == "sensor-corroborated" or e.sensor_derived


# ------------------------------------------------------------------------ metrics


def residual_duplicates(events: list[EventRow], cfg: BenchmarkConfig) -> int:
    """Events that might duplicate another of ours: same category and H3 res-5 cell,
    city-or-better precision, start within ``residual_duplicate_window_h`` of the
    previous one in that cell."""
    groups: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for e in events:
        if e.precision in cfg.city_or_better and e.h3_5 and e.t_start is not None:
            groups[(e.category, e.h3_5)].append(e.t_start)
    window = timedelta(hours=cfg.residual_duplicate_window_h)
    n = 0
    for starts in groups.values():
        starts.sort()
        n += sum(1 for a, b in pairwise(starts) if b - a <= window)
    return n


def duplication(inp: Inputs, cfg: BenchmarkConfig) -> dict:
    news = _news(inp)
    attached = [e for e in news if e.gdelt_rows > 0]
    rows = sum(e.gdelt_rows for e in attached)
    m = len(attached)
    resid = residual_duplicates(news, cfg)
    ours_epi = ratio(len(news), len(news) - resid) if news and len(news) > resid else None
    gdelt_rpi = ratio(rows, m)
    tracked_rows = sum(inp.lake_types["tracked"].values())
    return {
        "definition": DEFINITIONS["duplication"],
        "statement": (f"GDELT emitted {rows} rows for {m} incidents we merged into {m} "
                      f"events" if m else "No GDELT row attached to any of our events in "
                      "this window"),
        "ours": {
            "events": len(news),
            "articles": sum(e.articles for e in news),
            "articles_per_event": ratio(sum(e.articles for e in news), len(news)),
            "possible_residual_duplicates": resid,
            "events_per_incident_est": ours_epi,
        },
        "gdelt": {
            "rows_attached": rows,
            "incidents": m,
            "rows_per_incident": gdelt_rpi,
            "max_rows_per_incident": max((e.gdelt_rows for e in attached), default=0),
            # whole GDELT feed, our tracked CAMEO categories: rows per distinct
            # (date, event code, actors, place) key -> a lower bound of its duplication
            "window_rows_tracked": tracked_rows,
            "window_distinct_keys_tracked": inp.lake_keys["tracked"],
            "rows_per_distinct_key_tracked": ratio(tracked_rows, inp.lake_keys["tracked"]),
        },
        "dedup_ratio": (round(gdelt_rpi / ours_epi, 2)
                        if gdelt_rpi is not None and ours_epi else None),
    }


def geolocation(inp: Inputs, cfg: BenchmarkConfig) -> dict:
    news = _news(inp)
    ours = Counter(e.precision if e.precision in PREC_RANK else "unlocated" for e in news)
    located = sum(n for p, n in ours.items() if p != "unlocated")
    ours_cob = sum(ours[p] for p in cfg.city_or_better)

    def gdelt_dist(c: Counter) -> dict:
        total = sum(c.values())
        city = sum(c[t] for t in GDELT_CITY_TYPES)
        return {"rows": total,
                "by_type": {GDELT_GEO_TYPES.get(t, "unknown"): n
                            for t, n in sorted(c.items(), key=lambda kv: str(kv[0]))},
                "country_level_pct": pct(c["1"], total),
                "city_or_better_pct": pct(city, total)}

    centroid = Counter()
    for (reason, g), n in inp.rejected_geo.items():
        if g == "1":
            centroid[reason] += n
    prec = {e.id: e.precision for e in news}
    finer = same = coarser = 0
    for eid, g in inp.accepted_geo:
        if eid not in prec or g not in GDELT_TYPE_PRECISION:
            continue
        a, b = PREC_RANK.get(prec[eid], 0), PREC_RANK[GDELT_TYPE_PRECISION[g]]
        finer += a > b
        same += a == b
        coarser += a < b
    return {
        "definition": DEFINITIONS["geolocation"],
        "ours": {"events": len(news),
                 "by_precision": {p: ours.get(p, 0) for p in PRECISIONS},
                 "located_pct": pct(located, len(news)),
                 "country_level_pct": pct(ours["country"], len(news)),
                 "city_or_better_pct": pct(ours_cob, len(news)),
                 "city_or_better_pct_of_located": pct(ours_cob, located)},
        "gdelt": {"all": gdelt_dist(inp.lake_types["all"]),
                  "tracked": gdelt_dist(inp.lake_types["tracked"])},
        "gdelt_country_centroid_rejected": {"total": sum(centroid.values()),
                                            "by_reason": dict(centroid)},
        "matched_rows": {"ours_finer": finer, "same": same, "ours_coarser": coarser},
    }


def _latency_stats(pairs: list[tuple[float, float]]) -> dict:
    ours = [p[0] for p in pairs]
    gd = [p[1] for p in pairs]
    return {"pairs": len(pairs),
            "ours_median_min": quantile(ours, 0.5), "ours_p90_min": quantile(ours, 0.9),
            "gdelt_median_min": quantile(gd, 0.5), "gdelt_p90_min": quantile(gd, 0.9),
            "we_were_first_pct": pct(sum(1 for o, g in pairs if o < g), len(pairs))}


def latency(inp: Inputs, cfg: BenchmarkConfig) -> dict:
    cap = cfg.latency_max_h * 60.0
    live_pairs, all_pairs, ours_live = [], [], []
    for e in _news(inp):
        if e.first_article is None or e.first_seen is None:
            continue
        o = _minutes(e.first_seen, e.first_article)
        live = inp.live_since is None or e.first_article >= inp.live_since
        if live and abs(o) <= cap:
            ours_live.append(o)
        if e.first_gdelt is None:
            continue
        g = _minutes(e.first_gdelt, e.first_article)
        if abs(o) > cap or abs(g) > cap:
            continue
        all_pairs.append((o, g))
        if live:
            live_pairs.append((o, g))
    basis = "live" if live_pairs else "all"
    return {
        "definition": DEFINITIONS["latency"],
        "live_since": iso(inp.live_since) if inp.live_since else None,
        "basis": basis,
        "live": _latency_stats(live_pairs),
        "all": _latency_stats(all_pairs),
        "headline": _latency_stats(live_pairs if live_pairs else all_pairs),
        "ours_time_to_event_live": {"events": len(ours_live),
                                    "median_min": quantile(ours_live, 0.5),
                                    "p90_min": quantile(ours_live, 0.9)},
    }


def coverage(inp: Inputs, cfg: BenchmarkConfig) -> dict:
    news = _news(inp)
    both = [e for e in news if e.gdelt_rows > 0]
    only = [e for e in news if e.gdelt_rows == 0]
    rejected = {r: n for (d, r), n in inp.decisions.items() if d == "rejected"}
    relevant = sum(n for r, n in rejected.items() if r in GDELT_ONLY_RELEVANT)
    lake_rows = sum(inp.lake_types["all"].values())
    return {
        "definition": DEFINITIONS["coverage"],
        "ours_events": len(news),
        "both": len(both),
        "ours_only": len(only),
        "ours_only_pct": pct(len(only), len(news)),
        "ours_only_by_category": dict(Counter(e.category for e in only).most_common()),
        "gdelt_only_relevant": relevant,
        "gdelt_only_relevant_incidents_est": inp.gdelt_only_incidents,
        "gdelt_only_by_reason": dict(sorted(rejected.items(), key=lambda kv: -kv[1])),
        "gdelt_rows_in_window": lake_rows,
        "gdelt_rows_not_adjudicated": max(lake_rows - inp.lake_adjudicated, 0),
        "sensor_derived_events": sum(1 for e in inp.events if e.sensor_derived),
    }


def verification(inp: Inputs, cfg: BenchmarkConfig, min_domains: int = 2) -> dict:
    news = _news(inp)
    n = len(news)
    verdicts = Counter(e.verdict or "not_checked" for e in news)
    checked = n - verdicts["not_checked"]
    multi = sum(1 for e in news if e.source_diversity >= min_domains)
    corr = sum(1 for e in news if _corroborated(e))
    return {
        "definition": DEFINITIONS["verification"],
        "ours": {
            "events": n,
            "verdicts": {k: verdicts.get(k, 0) for k in
                         ("supported", "mismatch", "unsupported", "not_checked")},
            "checked_pct": pct(checked, n),
            "supported_pct": pct(verdicts["supported"], n),
            "supported_pct_of_checked": pct(verdicts["supported"], checked),
            "mismatch_pct_of_checked": pct(verdicts["mismatch"], checked),
            "unsupported_pct_of_checked": pct(verdicts["unsupported"], checked),
            "multi_source": multi,
            "multi_source_pct": pct(multi, n),
            "sensor_corroborated": corr,
            "sensor_corroborated_pct": pct(corr, n),
            "sensor_derived_events": sum(1 for e in inp.events if e.sensor_derived),
        },
        "gdelt": {"capability": (
            "GDELT 2.0 codes events from article text with no article-level verification, "
            "no confidence score and no sensor corroboration: 0% verified by construction.")},
    }


def verified_events(inp: Inputs) -> int:
    return sum(1 for e in inp.events
               if e.sensor_derived or e.verdict == "supported" or _corroborated(e))


def noise_rejection(inp: Inputs, cfg: BenchmarkConfig) -> dict:
    acc = sum(n for (d, _), n in inp.decisions.items() if d == "accepted")
    rej = {r: n for (d, r), n in inp.decisions.items() if d == "rejected"}
    total_rej = sum(rej.values())
    adjudicated = acc + total_rej
    return {
        "definition": DEFINITIONS["noise_rejection"],
        "adjudicated": adjudicated,
        "accepted": acc,
        "rejected": total_rej,
        "rejected_pct": pct(total_rej, adjudicated),
        # "stale" mostly reflects history replayed at our cold start, not GDELT quality
        "rejected_pct_excl_stale": pct(total_rej - rej.get("stale", 0),
                                       adjudicated - rej.get("stale", 0)),
        "by_reason": {r: {"rows": n, "pct": pct(n, adjudicated)}
                      for r, n in sorted(rej.items(), key=lambda kv: -kv[1])},
        "gdelt_rows_in_window": sum(inp.lake_types["all"].values()),
    }


def cost(inp: Inputs, cfg: BenchmarkConfig) -> dict:
    verified = verified_events(inp)
    alterlab = inp.alterlab_cost or 0.0
    spend = inp.llm_cost_window + alterlab
    notes = []
    if inp.alterlab_cost is None:
        notes.append("No AlterLab usage record exists yet: AlterLab spend counted as $0 "
                     "(news search is served from AlterLab's cache; article fetches for "
                     "extraction are not yet metered).")
    return {
        "definition": DEFINITIONS["cost"],
        "ours": {"llm_usd_window": round(inp.llm_cost_window, 6),
                 "llm_calls_window": inp.llm_calls_window,
                 "llm_usd_all_time": round(inp.llm_cost_total, 6),
                 "alterlab_usd_window": round(alterlab, 6),
                 "spend_usd_window": round(spend, 6),
                 "verified_events": verified,
                 "cost_per_verified_event_usd": (round(spend / verified, 4)
                                                 if verified else None)},
        "gdelt": {"capability": "GDELT data is free, but it produces no verified events, "
                                "so cost per verified event is undefined."},
        "notes": notes,
    }


def label_summary(labels: dict[str, dict], precisions: dict[str, str],
                  min_labels: int) -> dict:
    """Gold-set figures from human labels (``precisions``: event id -> precision)."""
    n = len(labels)
    out: dict = {"definition": DEFINITIONS["quality"], "labels": n, "min_labels": min_labels}
    if n < min_labels:
        out["status"] = "insufficient labels"
        return out
    ok_ev = sum(1 for v in labels.values() if v["correct_event"])
    ok_loc = sum(1 for v in labels.values() if v["correct_location"])
    by_prec: dict[str, dict] = {}
    for eid, v in labels.items():
        b = by_prec.setdefault(precisions.get(eid, "unknown"), {"labels": 0, "correct": 0})
        b["labels"] += 1
        b["correct"] += int(v["correct_location"])
    for b in by_prec.values():
        b["precision_at_location_pct"] = pct(b["correct"], b["labels"])
    out.update({"status": "ok", "event_correctness_pct": pct(ok_ev, n),
                "precision_at_location_pct": pct(ok_loc, n),
                "by_precision": by_prec})
    return out


def headline(m: dict) -> list[dict]:
    """The investor table: one row per pillar, Ours vs GDELT."""
    d, g, lat = m["duplication"], m["geolocation"], m["latency"]["headline"]
    cov, ver, noise, c = m["coverage"], m["verification"], m["noise_rejection"], m["cost"]
    return [
        {"metric": "duplication", "label": "Records per real-world incident",
         "ours": d["ours"]["events_per_incident_est"], "gdelt": d["gdelt"]["rows_per_incident"],
         "unit": "records/incident", "better": "lower"},
        {"metric": "geolocation", "label": "Located at city-or-better",
         "ours": g["ours"]["city_or_better_pct"],
         "gdelt": g["gdelt"]["tracked"]["city_or_better_pct"], "unit": "%",
         "better": "higher"},
        {"metric": "geolocation", "label": "Placed only at country level",
         "ours": g["ours"]["country_level_pct"],
         "gdelt": g["gdelt"]["tracked"]["country_level_pct"], "unit": "%", "better": "lower"},
        {"metric": "latency", "label": "Median delay after first article",
         "ours": lat["ours_median_min"], "gdelt": lat["gdelt_median_min"], "unit": "min",
         "better": "lower"},
        {"metric": "latency", "label": "Incidents where we were first",
         "ours": lat["we_were_first_pct"], "gdelt": None, "unit": "%", "better": "higher"},
        {"metric": "latency", "label": "Our live time-to-event (median)",
         "ours": m["latency"]["ours_time_to_event_live"]["median_min"], "gdelt": None,
         "unit": "min", "better": "lower", "gdelt_note": "15-min batches"},
        {"metric": "coverage", "label": "Incidents only one side has (in our categories)",
         "ours": cov["ours_only"],
         "gdelt": (cov["gdelt_only_relevant_incidents_est"]
                   if cov["gdelt_only_relevant_incidents_est"] is not None
                   else cov["gdelt_only_relevant"]),
         "unit": "count", "better": "context"},
        {"metric": "verification", "label": "Article-verified (supported)",
         "ours": ver["ours"]["supported_pct"], "gdelt": None, "unit": "%",
         "better": "higher", "gdelt_note": "not offered"},
        {"metric": "verification", "label": "Sensor-corroborated",
         "ours": ver["ours"]["sensor_corroborated_pct"], "gdelt": None, "unit": "%",
         "better": "higher", "gdelt_note": "not offered"},
        {"metric": "noise_rejection", "label": "GDELT rows rejected as noise",
         "ours": None, "gdelt": noise["rejected_pct_excl_stale"], "unit": "%",
         "better": "context", "gdelt_note": "excl. stale"},
        {"metric": "cost", "label": "Cost per verified event",
         "ours": c["ours"]["cost_per_verified_event_usd"], "gdelt": None, "unit": "USD",
         "better": "lower", "gdelt_note": "no verified events"},
    ]


def caveats(inp: Inputs, m: dict) -> list[str]:
    out = []
    news = m["verification"]["ours"]["events"]
    if news < 100:
        out.append(f"Small sample: {news} news events in the window; percentages are "
                   "indicative, not statistically robust.")
    if inp.live_since is not None and inp.live_since > inp.start:
        out.append(f"The platform has produced events only since {iso(inp.live_since)}; "
                   "the window start precedes that, so GDELT rows before it were never "
                   "adjudicated against our events.")
    lat = m["latency"]
    if lat["basis"] == "all" and lat["all"]["pairs"]:
        out.append("Latency uses events whose first article predates our live start "
                   "(cold-start backlog), so our delay is overstated.")
    out.append("Latency is measured only on incidents both systems have; GDELT rows that "
               "arrived before our event existed are rejected (no_article_backing) and never "
               "re-matched, which biases 'we were first' in our favour.")
    geo = m["geolocation"]["gdelt"]["tracked"]
    if geo["rows"]:
        out.append("GDELT's ActionGeo_Type is the precision GDELT claims, not verified "
                   "accuracy (same-name places are a known GDELT failure); our precision "
                   "is only assigned from places named in the article's own text. Accuracy "
                   "for both needs the labelled gold set.")
    cov = m["coverage"]
    if cov["gdelt_rows_in_window"] and cov["gdelt_rows_not_adjudicated"] >             cov["gdelt_rows_in_window"] / 2:
        out.append(f"{cov['gdelt_rows_not_adjudicated']} of {cov['gdelt_rows_in_window']} "
                   "GDELT rows in the window were never adjudicated (history loaded without "
                   "a cluster replay, or rows outside our relevance filter); overlap and "
                   "GDELT-only counts cover adjudicated rows only.")
    out.append("Events per incident for us is a proxy (possible residual duplicates); "
               "the labelled gold set is the authoritative measure.")
    q = m["quality"]
    if q.get("status") != "ok":
        out.append(f"Gold set: {q['labels']} labels (< {q['min_labels']}); event "
                   "correctness and precision@location are not yet measured.")
    out.extend(m["cost"]["notes"])
    return out


def score(inp: Inputs, cfg: BenchmarkConfig, window: str, *, min_domains: int = 2) -> dict:
    metrics = {
        "duplication": duplication(inp, cfg),
        "geolocation": geolocation(inp, cfg),
        "latency": latency(inp, cfg),
        "coverage": coverage(inp, cfg),
        "verification": verification(inp, cfg, min_domains),
        "noise_rejection": noise_rejection(inp, cfg),
        "cost": cost(inp, cfg),
        "quality": inp.labels or label_summary({}, {}, cfg.min_labels),
    }
    return {"as_of": iso(inp.now), "window": window, "window_start": iso(inp.start),
            "live_since": iso(inp.live_since) if inp.live_since else None,
            "headline": headline(metrics), "metrics": metrics,
            "caveats": caveats(inp, metrics)}


# -------------------------------------------------------------------------- collect

_EVENTS_SQL = """
SELECT e.id, e.category, e.precision, e.status, e.source_diversity, e.first_seen, e.t_start,
       e.title, e.place_name, e.h3_5, s.event_id IS NOT NULL, COALESCE(c.corroborated, 0),
       v.verdict
FROM events e
LEFT JOIN sensor_events s ON s.event_id = e.id
LEFT JOIN event_corroboration c ON c.event_id = e.id
LEFT JOIN event_verification v ON v.event_id = e.id
WHERE e.t_end >= ? AND e.origin != 'historical'
"""
_DOCS_SQL = """
SELECT d.event_id, SUM(d.role != 'gdelt'),
       MIN(CASE WHEN d.role != 'gdelt' THEN d.published_at END),
       MIN(CASE WHEN d.role = 'gdelt' THEN d.published_at END)
FROM event_documents d WHERE d.event_id IN (SELECT id FROM events WHERE t_end >= ?
                                            AND origin != 'historical')
GROUP BY d.event_id
"""
# in-window decisions plus every accepted row of a window event (duplication / geo pairs)
_DECISIONS_SQL = """
SELECT gdelt_event_id, decision, reason, event_id, decided_at >= ? AS in_window
FROM gdelt_decisions
WHERE decided_at >= ? OR (decision = 'accepted'
                          AND event_id IN (SELECT id FROM events WHERE t_end >= ?))
"""
_GEO = "json_extract_string(attrs, '$.geo_type')"
_ROOT = "json_extract_string(attrs, '$.event_root_code')"
_CODE = "json_extract_string(attrs, '$.event_code')"
_KEY = ("concat_ws('|', json_extract_string(attrs, '$.sqldate'), " + _CODE + ", "
        "json_extract_string(attrs, '$.actor1_code'), "
        "json_extract_string(attrs, '$.actor2_code'), "
        "json_extract_string(attrs, '$.geo_feature_id'))")
# approximate incident identity: event date, CAMEO root, place (feature id or name)
_INCIDENT = ("concat_ws('|', json_extract_string(attrs, '$.sqldate'), " + _ROOT + ", "
             "COALESCE(NULLIF(json_extract_string(attrs, '$.geo_feature_id'), ''), "
             "json_extract_string(attrs, '$.geo_name')))")


def _load_events(db, start: datetime) -> list[EventRow]:
    s = iso_db(start)
    rows: dict[str, EventRow] = {}
    for r in db.execute(_EVENTS_SQL, (s,)):
        rows[r[0]] = EventRow(
            id=r[0], category=r[1] or "", precision=r[2] or "unlocated", status=r[3] or "",
            source_diversity=int(r[4] or 0), first_seen=parse_ts(r[5]),
            t_start=parse_ts(r[6]), title=r[7] or "", place_name=r[8], h3_5=r[9],
            sensor_derived=bool(r[10]), corroborated=bool(r[11]), verdict=r[12])
    for eid, arts, fa, fg in db.execute(_DOCS_SQL, (s,)):
        e = rows.get(eid)
        if e is not None:
            e.articles, e.first_article, e.first_gdelt = int(arts or 0), parse_ts(fa), \
                parse_ts(fg)
    return list(rows.values())


def iso_db(dt: datetime) -> str:
    """Timestamp text comparable with the events tables (UTC, milliseconds)."""
    from osint.store.sqlite import iso as _iso

    return _iso(dt)


def collect(src: Sources, start: datetime, now: datetime,
            labels: dict | None = None) -> Inputs:
    inp = Inputs(now=now, start=start, labels=labels or {})
    gids: list[str] = []
    decided: list[str] = []
    reasons: list[str] = []
    evids: list[str | None] = []
    inwin: list[bool] = []
    with src.events_db() as db:
        if db is not None:
            inp.events = _load_events(db, start)
            row = db.execute("SELECT MIN(first_seen) FROM events WHERE origin = 'news' "
                             "AND id NOT IN (SELECT event_id FROM sensor_events)").fetchone()
            inp.live_since = parse_ts(row[0]) if row else None
            s = iso_db(start)
            accepted_count: Counter = Counter()
            cur = db.execute(_DECISIONS_SQL, (s, s, s))
            while batch := cur.fetchmany(5000):
                for gid, dec, reason, eid, in_window in batch:
                    if in_window:
                        inp.decisions[(dec, reason)] += 1
                    if dec == "accepted" and eid:
                        accepted_count[eid] += 1
                    gids.append(str(gid))
                    decided.append(dec)
                    reasons.append(reason)
                    evids.append(eid if dec == "accepted" else None)
                    inwin.append(bool(in_window))
            for e in inp.events:
                e.gdelt_rows = accepted_count.get(e.id, 0)
    _collect_lake(src, inp, gids, decided, reasons, evids, inwin)
    _collect_cost(src, inp)
    return inp


def _collect_lake(src: Sources, inp: Inputs, gids: list[str], decided: list[str],
                  reasons: list[str], evids: list[str | None], inwin: list[bool]) -> None:
    if src.lake is None:
        return
    dec = pa.table({"gid": pa.array(gids, pa.string()),
                    "decision": pa.array(decided, pa.string()),
                    "reason": pa.array(reasons, pa.string()),
                    "event_id": pa.array(evids, pa.string()),
                    "in_window": pa.array(inwin, pa.bool_())})
    tracked = src.tracked(_ROOT, _CODE)
    # 1) every GDELT row published in the window: geo type, tracked flag, dup key,
    #    adjudicated or not
    rows = src.lake_query(
        f"""
        WITH g AS (
          SELECT origin_id, {_GEO} AS geo, {tracked} AS tracked, {_KEY} AS k
          FROM documents
          WHERE kind = 'gdelt_event' AND published_at >= ? AND published_at <= ?),
        ids AS (SELECT DISTINCT gid FROM dec)
        SELECT GROUPING(geo), geo, tracked, COUNT(*), COUNT(DISTINCT k), COUNT(ids.gid)
        FROM g LEFT JOIN ids ON ids.gid = g.origin_id
        GROUP BY GROUPING SETS ((geo, tracked), (tracked))
        """, [inp.start, inp.now], start=inp.start, end=inp.now, tables={"dec": dec})
    for rollup, geo, is_tracked, n, keys, adjudicated in rows:
        if rollup:  # per-tracked-flag totals: distinct keys across geo types
            inp.lake_keys["all"] += keys
            if is_tracked:
                inp.lake_keys["tracked"] += keys
            continue
        g = str(geo) if geo is not None else "unknown"
        inp.lake_types["all"][g] += n
        inp.lake_adjudicated += adjudicated
        if is_tracked:
            inp.lake_types["tracked"][g] += n
    if not gids:
        return
    # 2) decided rows -> their GDELT geo type (rows decided up to a margin after DATEADDED)
    lo = inp.start - timedelta(hours=src.cfg.decision_join_margin_h)
    rows = src.lake_query(
        f"""
        WITH g AS (
          SELECT origin_id, any_value({_GEO}) AS geo FROM documents
          WHERE kind = 'gdelt_event' AND published_at >= ? GROUP BY origin_id)
        SELECT dec.decision, dec.reason, g.geo,
               CASE WHEN dec.decision = 'accepted' THEN dec.event_id END, COUNT(*)
        FROM dec LEFT JOIN g ON g.origin_id = dec.gid
        GROUP BY ALL
        """, [lo], start=lo, end=inp.now, tables={"dec": dec})
    for decision, reason, geo, eid, n in rows:
        g = str(geo) if geo is not None else None
        if decision == "accepted" and eid:
            inp.accepted_geo.extend([(eid, g)] * int(n))
        elif decision == "rejected":
            inp.rejected_geo[(reason, g)] += n
    # 3) GDELT-only rows in our scope -> approximate distinct incidents
    relevant = ", ".join(f"'{r}'" for r in GDELT_ONLY_RELEVANT)
    rows = src.lake_query(
        f"""
        WITH g AS (
          SELECT origin_id, any_value({_INCIDENT}) AS k FROM documents
          WHERE kind = 'gdelt_event' AND published_at >= ? GROUP BY origin_id)
        SELECT COUNT(DISTINCT g.k) FROM dec JOIN g ON g.origin_id = dec.gid
        WHERE dec.in_window AND dec.decision = 'rejected' AND dec.reason IN ({relevant})
        """, [lo], start=lo, end=inp.now, tables={"dec": dec})
    inp.gdelt_only_incidents = int(rows[0][0]) if rows else 0


def _collect_cost(src: Sources, inp: Inputs) -> None:
    with src.extraction_db() as db:
        if db is None:
            return
        try:
            n, c = db.execute("SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM llm_calls "
                              "WHERE ts >= ?", (inp.start.timestamp(),)).fetchone()
            inp.llm_calls_window, inp.llm_cost_window = int(n), float(c)
            inp.llm_cost_total = float(db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls").fetchone()[0])
        except Exception:  # older/partial extraction db: no cost data
            log.debug("extraction cost unavailable", exc_info=True)


# -------------------------------------------------------------------------- service


class BenchmarkService:
    """Computes scorecards (thread-safe), caches them per window and keeps snapshots."""

    def __init__(self, src: Sources, store: BenchmarkStore,
                 clock=utcnow) -> None:
        self.src = src
        self.cfg = src.cfg
        self.store = store
        self.clock = clock
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()  # one computation at a time (memory)
        self._alock: asyncio.Lock | None = None
        self.last_snapshot: str | None = None
        self.last_error: str | None = None

    def labels_summary(self) -> dict:
        labels = self.store.labels()
        precisions: dict[str, str] = {}
        ids = list(labels)
        with self.src.events_db() as db:
            if db is not None:
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    for eid, p in db.execute(
                            f"SELECT id, precision FROM events WHERE id IN "
                            f"({','.join('?' * len(chunk))})", chunk):
                        precisions[eid] = p or "unlocated"
        return label_summary(labels, precisions, self.cfg.min_labels)

    def compute(self, window: str, now: datetime | None = None) -> dict:
        hours = self.cfg.window_hours(window)
        now = now or self.clock()
        with self._lock:
            t0 = time.monotonic()
            inp = collect(self.src, now - timedelta(hours=hours), now, self.labels_summary())
            card = score(inp, self.cfg, window, min_domains=self.src.multi_source_min_domains)
            card["compute_s"] = round(time.monotonic() - t0, 2)
        self._cache[window] = (time.monotonic(), card)
        return card

    async def scorecard(self, window: str) -> dict:
        hit = self._cache.get(window)
        if hit is not None and time.monotonic() - hit[0] < self.cfg.cache_ttl_s:
            return hit[1]
        if self._alock is None:
            self._alock = asyncio.Lock()
        async with self._alock:
            hit = self._cache.get(window)
            if hit is not None and time.monotonic() - hit[0] < self.cfg.cache_ttl_s:
                return hit[1]
            return await asyncio.to_thread(self.compute, window)

    def snapshot(self, now: datetime | None = None) -> dict[str, int]:
        """Compute and persist every window (sequentially, to bound memory)."""
        now = now or self.clock()
        out = {}
        for window in self.cfg.windows:
            out[window] = self.store.save_snapshot(window, self.compute(window, now), now)
        self.store.prune(retention_cutoff(now, self.cfg.snapshot_retention_days))
        self.last_snapshot = iso(now)
        return out
