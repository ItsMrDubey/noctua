"""Country boards, the global board (hotspots), the US posture panel and group pages,
assembled from the Terror Watch store. ``build_scopes`` precomputes the indicator scopes in
the worker; the API reads them (and assembles group pages on request)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from osint.terror import baseline as ucdp
from osint.terror import countries, indicators
from osint.terror.definition import DEFINITION_VERSION

LIVE = ("confirmed", "reported")


def _since(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).isoformat()


def recent_designations(store, now: datetime, days: int = 90, *, group_ids=None,
                        cache: dict | None = None) -> list[dict]:
    """Designation events in the window: entries whose OFFICIAL listing / delisting date is
    within it, plus changes detected between fetches (lists without dates). Each states which
    date it is."""
    cut = (now - timedelta(days=days)).date().isoformat()
    cache = cache if cache is not None else {}
    if "all" not in cache:
        cache["all"] = store.designations(include_removed=True)
        cache["changes"] = store.designation_changes(
            since=(now - timedelta(days=400)).date().isoformat(), limit=5000)
    out = []
    for d in cache["all"]:
        if group_ids is not None and d.get("group_id") not in group_ids:
            continue
        for field, change in (("listed_on", "listed"), ("delisted_on", "delisted")):
            v = d.get(field)
            if v and v[:10] >= cut[: len(v[:10])]:
                out.append({"list_id": d["list_id"], "name": d["name"], "kind": d.get("kind"),
                            "change": change, "date": v, "date_basis": "official date on the "
                            "list", "group_id": d.get("group_id"), "ref": d["ref"]})
    for c in cache["changes"]:
        if (c.get("detected_at") or "")[:10] < cut and not c.get("official_date"):
            continue
        if group_ids is not None and c.get("group_id") not in group_ids:
            continue
        if c.get("official_date"):
            continue  # already above with its official date
        out.append({"list_id": c["list_id"], "name": c["name"], "kind": c.get("kind"),
                    "change": c["change"], "date": c["detected_at"][:10],
                    "date_basis": "first detected by this system (the list states no date)",
                    "group_id": c.get("group_id"), "ref": c["ref"]})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def _groups_in_country(store, cc: str, now: datetime) -> set[str]:
    cut = (now - timedelta(days=730)).strftime("%Y-%m")
    gids = {r["group_id"] for r in store.ucdp(country=cc) if r.get("group_id")
            and r["month"] >= cut}
    gids |= {i["group"]["group_id"] for i in store.incidents(country=cc, statuses=LIVE)
             if (i.get("group") or {}).get("kind") == "link"}
    return gids


def country_board(store, kb, cc: str, *, now: datetime, cfg, coverage_days: float,
                  calendar: list[dict], coverage=None, cache: dict | None = None) -> dict:
    coverage = coverage if coverage is not None else indicators.coverage_months(
        store.ucdp())
    rows = store.ucdp(country=cc)
    incs = store.incidents(country=cc, since=_since(now, 400), limit=5000)
    live = [i for i in incs if i["status"] in LIVE]
    levels = store.levels(cc)
    adv = (store.advisories(cc) or [None])[0]
    rates = indicators.base_rates(rows, coverage)
    tempo = indicators.ucdp_tempo(rows, coverage, recent_months=cfg.ucdp_recent_months,
                                  baseline_months=cfg.ucdp_baseline_months)
    deaths = indicators.ucdp_tempo(rows, coverage, recent_months=cfg.ucdp_recent_months,
                                   baseline_months=cfg.ucdp_baseline_months, key="deaths")
    news = indicators.news_tempo(incs, now, coverage_days, window_days=cfg.news_window_days)
    plots = indicators.news_tempo(incs, now, coverage_days, window_days=cfg.news_window_days,
                                  klass=("plot", "arrest", "prosecution"))
    gids = _groups_in_country(store, cc, now)
    desig = recent_designations(store, now, 90, group_ids=gids, cache=cache)
    claims = [i for i in live if (i.get("attribution") or {}).get("level") == "claimed"
              and (indicators._days_ago(i.get("sort_time"), now) or 1e9) < 30]
    today = indicators.utc_today(now)
    cal = [c for c in indicators.upcoming_dates(calendar, today)
           if not c.get("countries") or cc in c["countries"]]
    # 0 incidents in a 30-day window means nothing when news covers only a few days
    recent_n = news["observed"] if news and coverage_days >= cfg.news_window_days else None
    post = indicators.posture(levels=levels, advisory=adv, rates=rates, tempo=tempo,
                              recent_incidents=recent_n)
    groups = []
    for gid in sorted(gids):
        g = kb.get(gid)
        if g:
            groups.append({"id": gid, "name": g.name, "category": g.category})
    return {
        "scope": f"country:{cc}", "country": cc, "name": countries.name(cc) or cc,
        "definition_version": DEFINITION_VERSION,
        "official_levels": levels, "level_history": store.level_history(cc)[:10],
        "advisory": adv, "advisory_history": store.advisory_history(cc)[:10],
        "indicators": {
            "ucdp_events": tempo, "ucdp_deaths": deaths, "base_rates": rates,
            "news_incident_tempo": news, "news_plot_arrest_tempo": plots,
            "attack_mix": indicators.attack_mix(incs, now, coverage_days),
            "lethality_30d": indicators.lethality(incs, now),
            "claims_30d": {"count": len(claims), "basis": "incidents whose attribution level is "
                           "'claimed' (claim reported by the cited outlet; verbatim quote)"},
            "designations_90d": {"count": len(desig), "items": desig[:20],
                                 "basis": "designations / delistings of groups active in the "
                                          "country (UCDP or linked incidents, 24 months)"},
        },
        "posture": post, "calendar": cal, "groups": groups,
        "recent_incidents": [_slim(i) for i in live[:25]],
        "counts": {"attacks_30d": sum(1 for i in live if i.get("klass") == "attack" and
                                      (indicators._days_ago(i.get("sort_time"), now) or 1e9)
                                      < 30 and not i.get("follow_up")),
                   "possible": sum(1 for i in incs if i["status"] == "possible")},
        "disclaimer": "Indicators & warnings, not predictions.",
    }


def _slim(i: dict) -> dict:
    k = ((i.get("casualties") or {}).get("killed") or {})
    return {"id": i["id"], "event_id": i["event_id"], "title": i.get("title"),
            "klass": i.get("klass"), "status": i["status"], "country": i.get("country"),
            "place_name": i.get("place_name"), "precision": i.get("precision"),
            "lat": i.get("lat"), "lon": i.get("lon"), "attack_type": i.get("attack_type"),
            "attribution_level": (i.get("attribution") or {}).get("level"),
            "group": i.get("group"), "killed": k, "sort_time": i.get("sort_time"),
            "occurred_at": i.get("occurred_at"), "first_reported_at": i.get("first_reported_at"),
            "follow_up": i.get("follow_up"), "confidence": i.get("confidence"),
            "n_outlets": (i.get("outlets") or {}).get("n"), "languages": i.get("languages")}


def hotspots(store, kb, now: datetime, cfg, coverage) -> list[dict]:
    """Countries ranked by designated-group one-sided-violence deaths in the last
    ``ucdp_recent_months`` (UCDP), with each country's tempo test. Context, not a forecast."""
    if not coverage:
        return []
    rows = store.ucdp(designated_only=True)
    last = indicators._month_add(coverage[1], -1)  # newest Candidate month is preliminary
    first_recent = indicators._month_add(last, -(cfg.ucdp_recent_months - 1))
    per: dict[str, dict] = {}
    for r in rows:
        if r["month"] >= first_recent:
            p = per.setdefault(r["country"], {"events": 0, "deaths": 0, "groups": set()})
            p["events"] += r["events"]
            p["deaths"] += r["deaths_best"]
            if r.get("group_id"):
                p["groups"].add(r["group_id"])
    out = []
    for cc, p in sorted(per.items(), key=lambda kv: -kv[1]["deaths"])[:25]:
        t = indicators.ucdp_tempo(store.ucdp(country=cc), coverage,
                                  recent_months=cfg.ucdp_recent_months,
                                  baseline_months=cfg.ucdp_baseline_months)
        out.append({"country": cc, "name": countries.name(cc) or cc, "events": p["events"],
                    "deaths": p["deaths"], "window": f"{first_recent}..{last}",
                    "groups": [{"id": g, "name": (kb.get(g).name if kb.get(g) else g)}
                               for g in sorted(p["groups"])],
                    "tempo_verdict": t.get("verdict"), "expected_events": t.get("expected"),
                    "p_above": t.get("p_above")})
    return out


def global_board(store, kb, *, now: datetime, cfg, coverage_days: float, calendar,
                 coverage) -> dict:
    rows = store.ucdp()
    incs = store.incidents(since=_since(now, 400), limit=20000)
    live = [i for i in incs if i["status"] in LIVE]
    by_country: dict[str, int] = {}
    for i in live:
        if i.get("klass") == "attack" and not i.get("follow_up") and i.get("country") and \
                (indicators._days_ago(i.get("sort_time"), now) or 1e9) < 30:
            by_country[i["country"]] = by_country.get(i["country"], 0) + 1
    return {
        "scope": "global", "definition_version": DEFINITION_VERSION,
        "indicators": {
            "ucdp_events": indicators.ucdp_tempo(rows, coverage,
                                                 recent_months=cfg.ucdp_recent_months,
                                                 baseline_months=cfg.ucdp_baseline_months),
            "ucdp_deaths": indicators.ucdp_tempo(rows, coverage,
                                                 recent_months=cfg.ucdp_recent_months,
                                                 baseline_months=cfg.ucdp_baseline_months,
                                                 key="deaths"),
            "base_rates": indicators.base_rates(rows, coverage),
            "news_incident_tempo": indicators.news_tempo(incs, now, coverage_days,
                                                         window_days=cfg.news_window_days),
            "news_plot_arrest_tempo": indicators.news_tempo(
                incs, now, coverage_days, window_days=cfg.news_window_days,
                klass=("plot", "arrest", "prosecution")),
            "lethality_30d": indicators.lethality(incs, now),
            "designations_90d": {"items": recent_designations(store, now, 90)[:40]},
        },
        "hotspots_ucdp": hotspots(store, kb, now, cfg, coverage),
        "news_attacks_30d_by_country": [
            {"country": c, "name": countries.name(c) or c, "attacks": n}
            for c, n in sorted(by_country.items(), key=lambda kv: -kv[1])],
        "levels": store.levels(), "level_changes": store.level_history()[:20],
        "advisory_changes": store.advisory_history()[:30],
        "calendar": indicators.upcoming_dates(calendar, indicators.utc_today(now)),
        "news_coverage_days": round(coverage_days, 1),
        "counts": store.status_counts(),
        "disclaimer": "Indicators & warnings, not predictions.",
    }


def us_panel(store, kb, *, now: datetime, cfg, coverage_days, calendar, coverage) -> dict:
    """The US posture panel: the country board plus NTAS, DOJ terrorism cases and recent
    US designations (FTO / SDGT)."""
    board = country_board(store, kb, "US", now=now, cfg=cfg, coverage_days=coverage_days,
                          calendar=calendar, coverage=coverage)
    docs = store.docs(source="doj", since=_since(now, 400), limit=2000)
    st = store.source_state("doj_press") or {}
    first_doc = min((d.get("published_at") or "" for d in docs), default="")

    def n_since(days: int, kinds=None) -> int:
        cut = _since(now, days)
        return sum(1 for d in docs if (d.get("published_at") or "") >= cut and
                   (kinds is None or d.get("kind") in kinds))

    span_days = None
    if first_doc:
        span_days = (now - datetime.fromisoformat(first_doc)).days
    us_lists = [d for d in recent_designations(store, now, 365)
                if d["list_id"] in ("us_fto", "us_sdgt")]
    board["us"] = {
        "ntas": next((lv for lv in board["official_levels"]
                      if lv.get("authority", "").startswith("DHS")), None),
        "doj": {"cases_30d": n_since(30), "prosecutions_30d": n_since(30, {"prosecution"}),
                "arrests_plots_30d": n_since(30, {"arrest", "plot"}),
                "coverage_days": span_days, "last_fetch": st.get("last_ok"),
                "basis": "DOJ press releases tagged Counterterrorism / Domestic Terrorism by "
                         "DOJ, or NSD releases naming terrorism; deduplicated USAO/NSD copies",
                "baseline": "no baseline yet" if (span_days or 0) < 60 else "see series",
                "recent": docs[:12]},
        "designations_365d": {"fto": [d for d in us_lists if d["list_id"] == "us_fto"][:20],
                              "sdgt_detected": [d for d in us_lists
                                                if d["list_id"] == "us_sdgt"][:20]},
    }
    return board


def group_page(store, kb, gid: str, *, now: datetime) -> dict | None:
    g = kb.get(gid)
    if g is None:
        return None
    family = [gid, *kb.children(gid)]
    desig = []
    for fid in family:
        desig += [d for d in store.designations(group_id=fid, include_removed=True)]
    incs = []
    for fid in family:
        incs += store.incidents(group_id=fid, limit=500)
    incs.sort(key=lambda i: i.get("sort_time") or i.get("first_reported_at") or "",
              reverse=True)
    rows = []
    for fid in family:
        rows += store.ucdp(group_id=fid)
    cov = indicators.coverage_months(store.ucdp())
    areas: dict[str, dict] = {}
    cut = (now - timedelta(days=730)).strftime("%Y-%m")
    for r in rows:
        if r["month"] >= cut:
            a = areas.setdefault(r["country"], {"ucdp_events": 0, "ucdp_deaths": 0,
                                                "incidents": 0})
            a["ucdp_events"] += r["events"]
            a["ucdp_deaths"] += r["deaths_best"]
    for i in incs:
        if i.get("country") and i["status"] in LIVE and (i.get("group") or {}).get("kind") \
                == "link":
            a = areas.setdefault(i["country"], {"ucdp_events": 0, "ucdp_deaths": 0,
                                                "incidents": 0})
            a["incidents"] += 1
    lists = {}
    for d in desig:
        lst = lists.setdefault(d["list_id"], [])
        lst.append({k: d.get(k) for k in ("name", "status", "listed_on", "delisted_on",
                                          "group_basis", "removed_at", "ref", "source_url",
                                          "kind", "last_seen")})
    status_by_list = {}
    for lid, entries in lists.items():
        orgs = [e for e in entries if e.get("kind") in (None, "organisation")]
        pool = orgs or entries
        active = [e for e in pool if e["status"] == "listed" and not e.get("removed_at")]
        status_by_list[lid] = {"status": "listed" if active else "delisted/removed",
                               "entries": entries[:30], "n_entries": len(entries),
                               "persons_or_assets": sum(1 for e in entries if e.get("kind")
                                                        not in (None, "organisation"))}
    tempo = indicators.ucdp_tempo(rows, cov) if rows else {"available": False,
                                                           "basis": "no UCDP actor mapped"}
    return {
        "group": g.as_dict(), "family": [kb.get(f).as_dict() for f in family if kb.get(f)],
        "parent": kb.get(g.parent).as_dict() if g.parent and kb.get(g.parent) else None,
        "designations": status_by_list,
        "designation_basis": "official lists matched by exact name / alias (no similarity); "
                             "lists not shown have no matching entry",
        "areas_of_operation": [
            {"country": cc, "name": countries.name(cc) or cc, **v}
            for cc, v in sorted(areas.items(), key=lambda kv: -(kv[1]["ucdp_events"] +
                                                                 kv[1]["incidents"]))],
        "areas_basis": "derived from data (UCDP one-sided violence attributed to the group, "
                       "last 24 months, and linked incidents) -- not a curated claim",
        "ucdp_tempo": tempo, "incidents": [_slim(i) for i in incs[:60]],
        "incident_counts": {s: sum(1 for i in incs if i["status"] == s)
                            for s in ("confirmed", "reported", "possible")},
        "docs": [d for d in store.docs(limit=400) if gid in (d.get("group_ids") or [])][:20],
    }


def build_scopes(store, kb, *, now: datetime, cfg, coverage_days: float, calendar) -> dict:
    cov = indicators.coverage_months(store.ucdp())
    g = global_board(store, kb, now=now, cfg=cfg, coverage_days=coverage_days,
                     calendar=calendar, coverage=cov)
    store.save_indicators("global", g, now)
    us = us_panel(store, kb, now=now, cfg=cfg, coverage_days=coverage_days, calendar=calendar,
                  coverage=cov)
    store.save_indicators("country:US", us, now)
    ccs = {a["country"] for a in store.advisories()} | {lv["country"] for lv in store.levels()}
    ccs |= {r["country"] for r in store.ucdp(designated_only=True)
            if r["month"] >= (now - timedelta(days=1100)).strftime("%Y-%m")}
    ccs |= {i["country"] for i in store.incidents(statuses=("confirmed", "reported",
                                                            "possible"), limit=20000)
            if i.get("country")}
    n = 1
    cache: dict = {}
    for cc in sorted(c for c in ccs if c and c != "US"):
        b = country_board(store, kb, cc, now=now, cfg=cfg, coverage_days=coverage_days,
                          calendar=calendar, coverage=cov, cache=cache)
        store.save_indicators(f"country:{cc}", b, now)
        n += 1
    return {"scopes": n + 1, "ucdp_coverage": cov,
            "at": now.astimezone(UTC).isoformat(timespec="seconds")}


def ucdp_caveat() -> str:
    return ucdp.CAVEAT
