"""Indicators & warnings (I&W) per country and globally -- never predictions.

Every indicator states its source, window, baseline window and test; when the data is too
short for a baseline the indicator says so ("no baseline yet") instead of showing a ratio
(lesson 9). Counts are unique incidents, not articles (lesson 6).

Posture index (0-100), documented formula (``POSTURE_FORMULA``): a weighted mean of the
available components below, weights renormalised over the components that exist; the
uncertainty band is the 10th-90th percentile of 500 Monte Carlo draws (Poisson-gamma
posteriors for the count-based components, uniform 0-1 for missing components, so missing
data widens the band rather than lowering the score).
"""

from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime, timedelta

from osint.terror import baseline as ucdp
from osint.terror import stats

POSTURE_WEIGHTS = {"official_level": 0.30, "advisory": 0.15, "base_rate": 0.25,
                   "tempo": 0.20, "recent_incidents": 0.10}
POSTURE_FORMULA = {
    "official_level": "national authority's published level: rank / scale size (e.g. UK "
                      "SEVERE = 4/5). DHS NTAS has no standing level and is not used.",
    "advisory": "US State Dept travel advisory level / 4; halved when the advisory does not "
                "name terrorism as a risk.",
    "base_rate": "used only when the country has recorded events in the proxy; "
                 "log10(1 + mean annual deaths in UCDP one-sided violence by designated "
                 "non-criminal groups, last 5 complete years) / log10(1 + 5000), capped at 1.",
    "tempo": "UCDP designated-group events, last 3 months vs the 12 months before (negative "
             "binomial test): 0.5 when within normal variation; 0.5 + 0.5*min(1, log2(ratio)/2) "
             "when significantly above (p < 0.05); symmetric when below.",
    "recent_incidents": "confirmed + reported terrorist attacks from news in the last 30 "
                        "days (unique incidents): log10(1+n) / log10(31), capped at 1.",
    "combination": "weighted mean over available components (weights renormalised); band = "
                   "10th-90th percentile of 500 draws; missing components drawn uniformly.",
    "disclaimer": "An index of indicators, not a probability or forecast of attacks.",
}
MIN_NEWS_BASELINE_DAYS = 60


def _month_add(m: str, k: int) -> str:
    y, mo = int(m[:4]), int(m[5:7])
    idx = y * 12 + (mo - 1) + k
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_range(a: str, b: str) -> list[str]:
    out, m = [], a
    while m <= b:
        out.append(m)
        m = _month_add(m, 1)
    return out


# ------------------------------------------------------------------ UCDP tempo


def ucdp_tempo(rows: list[dict], coverage: tuple[str, str] | None, *, recent_months: int = 3,
               baseline_months: int = 12, key: str = "events") -> dict:
    """Designated-group one-sided violence: last ``recent_months`` vs the
    ``baseline_months`` before, monthly counts zero-filled over the dataset coverage."""
    if not coverage:
        return {"available": False, "basis": "UCDP baseline not loaded yet"}
    first, last = coverage
    # the newest UCDP Candidate month is preliminary (coding lag: it undercounts), so the
    # recent window ends one month before the end of coverage
    last = _month_add(last, -1)
    series = ucdp.monthly(rows)
    months = month_range(first, last)
    vals = [series.get(m, {}).get(key, 0) for m in months]
    if len(vals) < recent_months + baseline_months:
        return {"available": False, "basis": f"only {len(vals)} months of UCDP data"}
    recent = vals[-recent_months:]
    base = vals[-(recent_months + baseline_months):-recent_months]
    k = sum(recent)
    mean, var = stats.mean_var([float(x) for x in base])
    # the test is on the recent window's total: scale the monthly NB to the window
    exp = mean * recent_months
    test = stats.nb_test(k, exp, var * recent_months) if mean > 0 else stats.poisson_test(k, 0)
    lo, hi = stats.rate_ci(k, recent_months)
    verdict = stats.describe_p(test["p_high"], test["p_low"])
    return {"available": True, "metric": key, "recent_window": f"{months[-recent_months]}.."
            f"{last}", "baseline_window": f"{months[-(recent_months + baseline_months)]}.."
            f"{months[-recent_months - 1]}", "observed": k, "expected": round(exp, 1),
            "ratio": round(k / exp, 2) if exp > 0 else None,
            "recent_monthly_ci90": [round(lo, 1), round(hi, 1)],
            "baseline_monthly_mean": round(mean, 2), "baseline_monthly_sd": round(var ** 0.5, 2),
            "p_above": round(test["p_high"], 4), "p_below": round(test["p_low"], 4),
            "model": test["model"], "verdict": verdict, "series": dict(zip(months[-24:],
                                                                          vals[-24:])),
            "latest_month_note": f"the newest month ({coverage[1]}) is preliminary and left out "
                                 "of the windows; Candidate months may still be revised",
            "source": ucdp.LICENCE, "caveat": ucdp.CAVEAT}


def base_rates(rows: list[dict], coverage: tuple[str, str] | None, years: int = 5) -> dict:
    if not coverage:
        return {"available": False, "basis": "UCDP baseline not loaded yet"}
    ann = ucdp.annual(rows)
    last_full = int(coverage[1][:4]) - (0 if coverage[1][5:7] == "12" else 1)
    yrs = [str(y) for y in range(last_full - years + 1, last_full + 1)]
    table = [{"year": y, "events": ann.get(y, {}).get("events", 0),
              "deaths": ann.get(y, {}).get("deaths", 0)} for y in yrs]
    mean_deaths = sum(t["deaths"] for t in table) / len(table)
    mean_events = sum(t["events"] for t in table) / len(table)
    return {"available": True, "years": table, "mean_events": round(mean_events, 1),
            "mean_deaths": round(mean_deaths, 1), "source": ucdp.LICENCE,
            "caveat": ucdp.CAVEAT, "basis": "designated, non-criminal groups (KB mapping of "
            "UCDP actors)"}


# ------------------------------------------------------------------ news incidents


def _days_ago(ts: str | None, now: datetime) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (now - dt).total_seconds() / 86400


def news_tempo(incidents: list[dict], now: datetime, coverage_days: float, *,
               window_days: int = 30, klass: tuple[str, ...] = ("attack",)) -> dict:
    """Unique news-derived incidents in the last ``window_days`` (by event time, follow-ups
    and merged duplicates excluded) against the preceding windows -- only when the platform
    has collected news for long enough."""
    rel = [i for i in incidents if i.get("klass") in klass and not i.get("follow_up")
           and i.get("status") in ("confirmed", "reported")]
    ages = [a for a in (_days_ago(i.get("sort_time"), now) for i in rel) if a is not None]
    recent = sum(1 for a in ages if 0 <= a < window_days)
    confirmed = sum(1 for i, a in zip(rel, ages, strict=False)
                    if a is not None and 0 <= a < window_days and i["status"] == "confirmed")
    out = {"window_days": window_days, "observed": recent, "confirmed": confirmed,
           "reported_unconfirmed": recent - confirmed, "coverage_days": round(coverage_days, 1),
           "classes": list(klass)}
    if coverage_days < max(MIN_NEWS_BASELINE_DAYS, 2 * window_days):
        out.update(available=False, verdict="no baseline yet",
                   basis=f"news collection covers {coverage_days:.0f} days; a baseline needs "
                         f">= {max(MIN_NEWS_BASELINE_DAYS, 2 * window_days)} days")
        return out
    base_days = min(coverage_days - window_days, 365)
    base_n = sum(1 for a in ages if window_days <= a < window_days + base_days)
    exp = base_n * window_days / base_days
    test = stats.poisson_test(recent, exp)
    out.update(available=True, baseline_days=round(base_days), expected=round(exp, 2),
               p_above=round(test["p_high"], 4), p_below=round(test["p_low"], 4),
               verdict=stats.describe_p(test["p_high"], test["p_low"]), model=test["model"])
    return out


def attack_mix(incidents: list[dict], now: datetime, coverage_days: float,
               window_days: int = 30) -> dict:
    rel = [i for i in incidents if i.get("klass") == "attack" and not i.get("follow_up")
           and i.get("status") in ("confirmed", "reported")]
    recent: dict[str, int] = {}
    base: dict[str, int] = {}
    for i in rel:
        a = _days_ago(i.get("sort_time"), now)
        if a is None:
            continue
        t = i.get("attack_type") or "unknown"
        if a < window_days:
            recent[t] = recent.get(t, 0) + 1
        else:
            base[t] = base.get(t, 0) + 1
    out = {"recent": recent, "baseline": base, "window_days": window_days}
    if coverage_days < max(MIN_NEWS_BASELINE_DAYS, 2 * window_days):
        out.update(available=False, verdict="no baseline yet",
                   basis=f"news collection covers {coverage_days:.0f} days")
        return out
    test = stats.chi2_shift(recent, base)
    if test is None:
        out.update(available=False, verdict="too few incidents for a test")
        return out
    out.update(available=True, **test, verdict="mix shifted" if test["p"] < 0.05 else
               "no significant shift")
    return out


def lethality(incidents: list[dict], now: datetime, window_days: int = 30) -> dict:
    rel = [i for i in incidents if i.get("klass") == "attack" and not i.get("follow_up")
           and i.get("status") in ("confirmed", "reported")
           and (_days_ago(i.get("sort_time"), now) or 1e9) < window_days]
    lows = [((i.get("casualties") or {}).get("killed") or {}).get("low") for i in rel]
    highs = [((i.get("casualties") or {}).get("killed") or {}).get("high") for i in rel]
    known = [x for x in lows if x is not None]
    return {"window_days": window_days, "incidents": len(rel),
            "with_figures": len(known), "killed_low_sum": sum(known),
            "killed_high_sum": sum(x for x in highs if x is not None),
            "basis": "sum over unique incidents of the lowest / highest reported figure; "
                     "incidents without figures are not counted as zero deaths"}


# ------------------------------------------------------------------ posture


def _level_component(levels: list[dict]) -> tuple[float | None, str | None]:
    best = None
    for lv in levels:
        if lv.get("authority", "").startswith("DHS"):
            continue  # NTAS: no standing level
        if lv.get("rank") and lv.get("scale_max") and (lv.get("scope") or "national") == \
                "national":
            v = lv["rank"] / lv["scale_max"]
            if best is None or v > best[0]:
                best = (v, f"{lv['authority']}: {lv['level']} ({lv['rank']}/{lv['scale_max']})")
    return best if best else (None, None)


def posture(*, levels: list[dict], advisory: dict | None, rates: dict, tempo: dict,
            recent_incidents: int | None, seed: int = 7) -> dict:
    comps: dict[str, dict] = {}
    v, basis = _level_component(levels)
    if v is not None:
        comps["official_level"] = {"value": round(v, 3), "basis": basis}
    if advisory and advisory.get("level"):
        av = advisory["level"] / 4 * (1.0 if advisory.get("terrorism") else 0.5)
        comps["advisory"] = {"value": round(av, 3), "basis": f"US State Dept Level "
                             f"{advisory['level']}" + (" (names terrorism)" if
                                                       advisory.get("terrorism") else "")}
    cap = math.log10(1 + 5000)
    # a country with no recorded group violence in the proxy is "not informative", not "0":
    # the UCDP proxy omits lone actors and non-lethal attacks (most terrorism in the West)
    if rates.get("available") and sum(y["events"] for y in rates["years"]) > 0:
        comps["base_rate"] = {"value": round(min(1.0, math.log10(1 + rates["mean_deaths"]) / cap),
                                             3),
                              "basis": f"mean {rates['mean_deaths']} deaths/yr (UCDP proxy, "
                                       f"{rates['years'][0]['year']}-{rates['years'][-1]['year']})"}
    if tempo.get("available") and (tempo["observed"] or tempo["expected"]):
        comps["tempo"] = {"value": round(_tempo_value(tempo["observed"], tempo["expected"],
                                                      tempo["p_above"], tempo["p_below"]), 3),
                          "basis": f"{tempo['verdict']} ({tempo['observed']} vs "
                                   f"{tempo['expected']} expected)"}
    if recent_incidents is not None:
        comps["recent_incidents"] = {
            "value": round(min(1.0, math.log10(1 + recent_incidents) / math.log10(31)), 3),
            "basis": f"{recent_incidents} unique attacks (30 d, news)"}
    avail = sum(POSTURE_WEIGHTS[k] for k in comps)
    if avail == 0:
        return {"available": False, "basis": "no component available", "components": comps,
                "formula": POSTURE_FORMULA}
    point = sum(POSTURE_WEIGHTS[k] * c["value"] for k, c in comps.items()) / avail
    rng = random.Random(seed)
    draws = []
    for _ in range(500):
        tot = 0.0
        for k, w in POSTURE_WEIGHTS.items():
            if k not in comps:
                tot += w * rng.random()
            elif k == "base_rate":
                n_years = len(rates["years"])
                lam = stats.gamma_sample(int(rates["mean_deaths"] * n_years), n_years, 1, rng)[0]
                tot += w * min(1.0, math.log10(1 + lam) / cap)
            elif k == "tempo":
                obs = stats.gamma_sample(tempo["observed"], 1, 1, rng)[0]
                exp = stats.gamma_sample(round(tempo["expected"] * 4), 4, 1, rng)[0]
                tot += w * _tempo_value(obs, exp, tempo["p_above"], tempo["p_below"])
            elif k == "recent_incidents":
                lam = stats.gamma_sample(recent_incidents or 0, 1, 1, rng)[0]
                tot += w * min(1.0, math.log10(1 + lam) / math.log10(31))
            else:
                tot += w * comps[k]["value"]
        draws.append(tot)
    draws.sort()
    idx = round(100 * point)
    # the band always contains the point estimate (missing components can pull draws away)
    band = [min(idx, round(100 * draws[49])), max(idx, round(100 * draws[449]))]
    return {"available": True, "index": idx, "band": band, "coverage": round(avail, 2),
        "components": comps, "weights": POSTURE_WEIGHTS, "formula": POSTURE_FORMULA,
        "label": "posture index (indicator composite, not a prediction)"}


def _tempo_value(obs: float, exp: float, p_above: float, p_below: float) -> float:
    if exp <= 0:
        return 0.5 if obs <= 0 else 1.0
    ratio = max(obs, 1e-6) / exp
    if p_above < 0.05 and ratio > 1:
        return 0.5 + 0.5 * min(1.0, math.log2(ratio) / 2)
    if p_below < 0.05 and ratio < 1:
        return 0.5 - 0.5 * min(1.0, -math.log2(ratio) / 2)
    return 0.5


# ------------------------------------------------------------------ calendar


def upcoming_dates(calendar: list[dict], today: date, days: int = 45) -> list[dict]:
    """Significant dates in the next ``days`` (context only; an anniversary is not an
    indicator of an attack)."""
    out = []
    for c in calendar:
        if c.get("date"):  # one-off, YYYY-MM-DD
            try:
                d = date.fromisoformat(c["date"])
            except ValueError:
                continue
        elif c.get("month_day"):
            mm, dd = (int(x) for x in c["month_day"].split("-"))
            try:
                d = date(today.year, mm, dd)
                if d < today:
                    d = date(today.year + 1, mm, dd)
            except ValueError:
                continue
        else:
            continue
        delta = (d - today).days
        if 0 <= delta <= days:
            out.append({**c, "on": d.isoformat(), "in_days": delta})
    out.sort(key=lambda x: x["in_days"])
    return out


def coverage_months(rows: list[dict]) -> tuple[str, str] | None:
    months = [r["month"] for r in rows]
    return (min(months), max(months)) if months else None


def utc_today(now: datetime) -> date:
    return now.astimezone(UTC).date()


def since_iso(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).isoformat()
