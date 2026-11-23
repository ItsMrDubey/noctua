"""Incident classification: an event (+ its article extraction + the terror extraction) ->
an incident record with class, status, attribution level, location and casualties, each
with its basis. Pure and deterministic given its inputs (the LLM output is an input).

Decision order (first match wins; every step that fires is listed in ``rules``):

1. Denial of the incident itself (headline rule or extraction ``occurred = no``) ->
   excluded. A denial of involvement does not count (lesson 1).
2. Article verification says the article reports no concrete event, and the terror
   extraction does not contradict it -> excluded.
3. No terror extraction yet: a state-warfare headline with no terrorism marker -> excluded
   (no LLM call spent); a non-candidate -> not an incident; otherwise pending.
4. Kind from the extraction; warfare / crime / communal / commentary -> excluded with the
   reason; ct_operation -> context only.
5. Attacks: the GTD-aligned definition (``osint.terror.definition``): state perpetrators
   excluded; >= 2 of C1-C3 with a sub-national perpetrator -> meets; insurgent attack on
   security forces in a conflict zone -> possible; unknown criteria -> possible.
6. Status: meets + >= 2 independent outlets (reposts and same-organisation feeds collapsed,
   all Telegram = one) and no article/location contradiction -> confirmed; otherwise
   reported (lessons 2, 3).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from osint.terror import countries, outlets, text
from osint.terror.definition import ATTACK_TYPES, TARGET_TYPES

Geocoder = Callable[[str, str, str], tuple[float, float, str, str] | None]
PREC_ORDER = ["point", "city", "admin1", "country", "unknown"]
MAP_PRECISIONS = {"point", "city", "admin1"}
# targets that are people / civilian institutions; transport and energy infrastructure are
# dual-use and never imply C3 on their own
CIVILIAN_TARGETS = {"civilians", "religious", "education", "journalists", "business",
                    "diplomatic", "western_interests"}
_ANON = re.compile(r"unidentified|unknown|gunmen|assailants|attackers|militants|armed men|"
                   r"\bsuspects?\b|\bindividuals?\b|\bpeople\b", re.IGNORECASE)
KIND_CLASS = {"attack": "attack", "attempted_attack": "attack", "plot_foiled": "plot",
              "arrest": "arrest", "prosecution": "prosecution", "threat_or_hoax": "threat",
              "ct_operation": "ct_operation"}
EXCLUDED_KINDS = {"warfare": "warfare / state or inter-state armed conflict",
                  "crime": "ordinary crime (no political, religious or social motive stated)",
                  "communal_violence": "communal / inter-communal violence",
                  "protest_or_unrest": "protest / civil unrest",
                  "commentary": "commentary / analysis (no new incident)",
                  "none": "no incident in the article"}
FOLLOW_UP_DAYS = 7


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        m = re.match(r"(\d{4})-(\d{2})(?:-(\d{2}))?", str(s))
        if not m:
            return None
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _finer(a: str, b: str) -> str:
    """The coarser of two precisions (never claim more precision than the weakest basis)."""
    return a if PREC_ORDER.index(a) >= PREC_ORDER.index(b) else b


def _first_reported(ev: dict) -> str | None:
    pubs = [d.get("published_at") for d in ev.get("documents") or [] if d.get("published_at")]
    return min(pubs) if pubs else ev.get("t_start")


# ------------------------------------------------------------------ location


def resolve_location(ev: dict, terr: dict | None, base: dict | None,
                     geocode: Geocoder | None) -> dict:
    """Attack location from the extraction's ``where`` (the incident description), with
    precision and basis. A capital-city metonym never becomes the attack site; a place with
    no verbatim quote is capped at country precision (lesson 4)."""
    out = {"lat": None, "lon": None, "precision": "unknown", "place_name": None,
           "country": None, "basis": "no location stated"}
    where = (terr or {}).get("where") or {}
    place, quote = where.get("place_name"), where.get("quote")
    country = where.get("country")
    title = ev.get("title") or ""
    if place and (text.is_metonym(place, quote or "") or
                  (not quote and text.is_metonym(place, title))):
        out["basis"] = (f"'{place}' rejected: used for its government (metonym), not as the "
                        "incident location")
        place = None
    if place and not quote:
        out["basis"] = (f"'{place}' stated by the extraction without a verbatim quote: capped "
                        "at country precision")
        place = None
    ev_cc0 = (ev.get("country") or "").upper() or None
    # with no stated country, the event's own country (if any) constrains the gazetteer; with
    # neither, a bare place name is ambiguous (Khar, Tirah, Algeciras exist in many countries)
    constraint = country or ev_cc0
    if place and not constraint:
        out["basis"] = (f"'{place}' named without a country: ambiguous, not placed (the "
                        "gazetteer would have to guess)")
        out["place_name"] = place
        place = None
    if place:
        prec = where.get("precision") or "unknown"
        hit = geocode(place, where.get("admin1") or "", constraint or "") if geocode else None
        if hit is not None and not country and hit[2] != constraint:
            out["basis"] = (f"'{place}' geocodes to {hit[2]}, outside the event's country "
                            f"{constraint}: not placed")
            hit = None
            place = None
        if hit is not None:
            lat, lon, gcc, gprec = hit
            if country and gcc and gcc != country:
                out["basis"] = (f"'{place}' geocodes to {gcc}, extraction says {country}: "
                                "ambiguous, kept at country precision")
            else:
                out.update(lat=round(lat, 5), lon=round(lon, 5),
                           precision=_finer(prec if prec != "unknown" else gprec, gprec),
                           place_name=place, country=gcc or constraint,
                           basis=f"incident description: \"{quote[:160]}\" -> gazetteer "
                                 f"({gprec})")
                return out
        elif place:
            out["basis"] = f"'{place}' not found in the gazetteer: country precision"
    # fallback: the event's own location, only when the article extraction supports it and
    # it agrees with the extracted country
    bres = (base or {}).get("result") or {}
    bverdict = (base or {}).get("verdict")
    ev_cc = (ev.get("country") or "").upper() or None
    if (bverdict == "supported" and ev.get("lat") is not None and ev.get("precision") in
            MAP_PRECISIONS and (not country or country == ev_cc)
            and not text.is_metonym(ev.get("place_name"), title)):
        out.update(lat=ev["lat"], lon=ev["lon"], precision=ev.get("precision"),
                   place_name=ev.get("place_name"), country=ev_cc,
                   basis="event location (supported by the article extraction; "
                         f"{(bres.get('where') or {}).get('place_name') or 'place'} )")
        return out
    cc = country or (ev_cc if not where else None)
    if cc:
        out.update(country=cc, precision="country",
                   basis=out["basis"] if out["basis"] != "no location stated" else
                   "country stated in the incident description")
    return out


# ------------------------------------------------------------------ casualties


def casualties(ev: dict, terr: dict | None, base: dict | None) -> dict:
    """Killed / injured with every source kept: the terror extraction, the event extraction
    and English headline figures per outlet. Range = min..max over sources; disagreement is
    flagged, never averaged away (lesson 10)."""
    srcs: list[dict] = []
    tc = (terr or {}).get("casualties") or {}
    if tc.get("killed") is not None or tc.get("injured") is not None:
        srcs.append({"source": "terror extraction (best article)", "killed": tc.get("killed"),
                     "killed_max": tc.get("killed_max"), "injured": tc.get("injured"),
                     "injured_max": tc.get("injured_max"), "quote": tc.get("quote"),
                     "verified": bool(tc.get("quote"))})
    bc = ((base or {}).get("result") or {}).get("casualties") or {}
    if bc.get("killed") is not None or bc.get("injured") is not None:
        srcs.append({"source": "event extraction", "killed": bc.get("killed"),
                     "injured": bc.get("injured"), "quote": None, "verified": False})
    seen = set()
    for d in ev.get("documents") or []:
        hc = text.headline_casualties(d.get("title"))
        key = (outlets.outlet(d), hc.get("killed"), hc.get("injured"))
        if hc and key not in seen:
            seen.add(key)
            srcs.append({"source": f"headline ({outlets.outlet(d) or d.get('source')})",
                         "killed": hc.get("killed"), "injured": hc.get("injured"),
                         "quote": d.get("title"), "verified": True,
                         "published_at": d.get("published_at")})

    def rng(key: str) -> dict:
        vals = [s[key] for s in srcs if s.get(key) is not None]
        vals += [s[key + "_max"] for s in srcs if s.get(key + "_max") is not None]
        if not vals:
            return {"low": None, "high": None, "disagreement": False}
        return {"low": min(vals), "high": max(vals), "disagreement": len(set(vals)) > 1}

    return {"killed": rng("killed"), "injured": rng("injured"), "sources": srcs,
            "includes_perpetrators": tc.get("includes_perpetrators")}


# ------------------------------------------------------------------ main


def classify(ev: dict, base: dict | None, terr: dict | None, *, kb=None,
             geocode: Geocoder | None = None, now: datetime | None = None) -> dict | None:
    """Incident record for one event, or None when the event is not a candidate at all."""
    now = now or datetime.now(UTC)
    title = ev.get("title") or ""
    docs = ev.get("documents") or []
    titles = " \n ".join([title, *[d.get("title") or "" for d in docs]])
    rules: list[str] = []
    first = _first_reported(ev)
    rec = {"id": "ti-" + ev["id"], "event_id": ev["id"], "title": title,
           "summary": (terr or {}).get("summary") or "", "klass": None, "status": None,
           "excluded_reason": None, "rules": rules, "first_reported_at": first,
           "last_reported_at": max([d.get("published_at") or "" for d in docs] or
                                   [ev.get("t_end") or ""]) or None,
           "event_category": ev.get("category"), "event_status": ev.get("status"),
           "llm": None}

    def exclude(reason: str) -> dict:
        rec.update(status="excluded", excluded_reason=reason)
        return rec

    threat_item = bool(text.THREAT.search(title))  # a hoax threat is a threat item, not a denial
    if text.is_denial(title) and not threat_item:
        rules.append("denial_headline")
        rec["klass"] = "attack"
        return exclude(f"denial: the report says the incident did not happen (\"{title[:160]}\")")
    base_res = (base or {}).get("result") or {}
    if terr is not None:
        rec["llm"] = {"kind": terr.get("kind"), "occurred": terr.get("occurred"),
                      "unverified": terr.get("unverified") or [],
                      "model": terr.get("_model"), "text_source": terr.get("text_source")}
        if terr.get("occurred") == "no" and terr.get("kind") != "threat_or_hoax":
            rules.append("denial_extraction")
            q = terr.get("negation_quote") or ""
            return exclude("denial: the article says the incident did not happen"
                           + (f" (\"{q[:160]}\")" if q else ""))
    if base is not None and base.get("status", "done") == "done" and \
            base_res.get("is_real_event") is False and (terr is None or
                                                       terr.get("occurred") != "yes"):
        rules.append("verdict_unsupported")
        return exclude("article verification: the article reports no concrete event")
    if terr is None:
        if text.state_warfare_only(title):
            rules.append("state_warfare_headline")
            rec["klass"] = "attack"
            return exclude("warfare: the headline actor is a state's armed forces and no "
                           "terrorism marker is present (rule, no LLM call)")
        if not text.is_candidate(titles):
            return None
        rec.update(status="pending", klass=None)
        rules.append("candidate_pending_llm")
        return rec

    kind = terr.get("kind") or "none"
    flags = text.flags(titles)
    perp = terr.get("perpetrator") or {}
    crit = terr.get("criteria") or {}
    rec["attack_type"] = terr.get("attack_type") or "unknown"
    rec["target_type"] = terr.get("target_type") or "unknown"
    rec["attack_type_label"] = ATTACK_TYPES.get(rec["attack_type"], rec["attack_type"])
    rec["target_type_label"] = TARGET_TYPES.get(rec["target_type"], rec["target_type"])
    if kind in EXCLUDED_KINDS:
        # a "warfare" label on a non-state attack deliberately targeting civilians is
        # re-examined below instead of being dropped
        civ = crit.get("civilians_or_noncombatants_targeted") is True
        if not (kind == "warfare" and perp.get("type") in ("non_state_group", "lone_actor")
                and civ):
            rules.append(f"kind_{kind}")
            rec["klass"] = "attack" if kind in ("warfare", "crime", "communal_violence") \
                else None
            return exclude(EXCLUDED_KINDS[kind])
        rules.append("warfare_label_overridden_nonstate_civilian_target")
        kind = "attack"
    klass = KIND_CLASS.get(kind)
    rec["klass"] = klass
    if kind == "attempted_attack":
        rules.append("attempted")
    rec["attempted"] = kind == "attempted_attack"

    # ---------------------------------------------------------- attribution + group
    attr = terr.get("attribution") or {}
    level = attr.get("level") or "unknown"
    if level != "unknown" and not attr.get("quote"):
        rules.append("attribution_unverified")
        level_basis = (f"the extraction said '{level}' but gave no verbatim quote: treated "
                       "as unknown")
        level = "unknown"
    else:
        level_basis = f"verbatim: \"{attr.get('quote')}\"" if attr.get("quote") else \
            "no perpetrator / claim stated"
    if level == "unknown" and perp.get("quote") and perp.get("name") and \
            perp.get("type") in ("non_state_group", "lone_actor") and \
            not _ANON.search(perp.get("name") or ""):
        # the outlet itself names the perpetrator (no claim and no official attribution quoted)
        rules.append("perpetrator_named_by_outlet")
        level = "suspected"
        level_basis = f"perpetrator named by the reporting outlet: \"{perp['quote']}\""
    link = None
    if kb is not None:
        quotes = [q for q in (attr.get("quote"), perp.get("quote")) if q]
        link = kb.link(perpetrator=perp.get("name"), quotes=quotes, level=level)
        if link:
            g = kb.get(link["group_id"])
            link["group_name"] = g.name if g else link["group_id"]
            link["category"] = g.category if g else None
    rec["attribution"] = {"level": level, "by": attr.get("by"), "channel": attr.get("channel"),
                          "quote": attr.get("quote"), "basis": level_basis}
    rec["perpetrator"] = {"name": perp.get("name"), "type": perp.get("type"),
                          "quote": perp.get("quote")}
    rec["group"] = link
    rec["motive"] = terr.get("motive")

    # ---------------------------------------------------------- time
    occurred = _parse_ts(terr.get("when")) if terr.get("when_quote") else None
    first_dt = _parse_ts(first)
    if occurred and first_dt and occurred > first_dt + timedelta(days=1):
        rules.append("when_after_first_report_ignored")
        occurred = None
    follow = bool(terr.get("is_follow_up")) or bool(
        occurred and first_dt and (first_dt - occurred).days > FOLLOW_UP_DAYS)
    if follow:
        rules.append("follow_up_coverage")
    rec["follow_up"] = follow
    rec["occurred_at"] = occurred.isoformat() if occurred else None
    rec["occurred_basis"] = (f"stated in the article: \"{terr.get('when_quote')}\"" if occurred
                             else "not stated; first report time used for ordering"
                             + (" (follow-up coverage of an earlier incident)" if follow else ""))
    rec["sort_time"] = rec["occurred_at"] or (None if follow else first)

    # ---------------------------------------------------------- location + casualties
    rec["location"] = resolve_location(ev, terr, base, geocode)
    rec.update({k: rec["location"][k] for k in ("lat", "lon", "precision", "place_name",
                                                "country")})
    rec["country_name"] = countries.name(rec["country"])
    rec["casualties"] = casualties(ev, terr, base)

    # ---------------------------------------------------------- outlets
    ind = outlets.independent_outlets(docs)
    rec["outlets"] = ind
    rec["languages"] = sorted({(d.get("language") or "").split("-")[0] for d in docs} - {""})

    # ---------------------------------------------------------- definition
    if klass == "ct_operation":
        rules.append("ct_operation_context")
        rec["status"] = "context"
        rec["definition"] = {"met": False, "basis": "counter-terrorism operation (context)"}
        return _finish(rec, ind, base, rules)
    if klass in ("attack", "plot") and perp.get("type") in ("state_military", "state_security"):
        rules.append("state_perpetrator")
        return exclude("state actor: acts by a state's armed forces or security services are "
                       "outside the definition")
    if flags.not_terror:
        rules.append("authorities_not_terror")
        if not crit.get("political_religious_social_goal"):
            return exclude("authorities state it is not terrorism-related")
    group_cat = (link or {}).get("category") if (link or {}).get("kind") == "link" else None
    goal = crit.get("political_religious_social_goal")
    goal_basis = "extraction (from the text)" if goal is not None else None
    if goal is None and group_cat and group_cat != "criminal":
        goal, goal_basis = True, f"perpetrator linked to a {group_cat} group (verbatim quote)"
    audience = crit.get("intimidate_wider_audience")
    civ = crit.get("civilians_or_noncombatants_targeted")
    if civ is None and rec["target_type"] in CIVILIAN_TARGETS:
        civ = True
    elif civ is None and rec["target_type"] == "security_forces":
        civ = False
    c = {"C1_goal": goal, "C2_audience": audience, "C3_outside_warfare": civ}
    n_true = sum(v is True for v in c.values())
    n_unknown = sum(v is None for v in c.values())
    sub_national = perp.get("type") in ("non_state_group", "lone_actor") or bool(
        group_cat and group_cat not in ("criminal",))
    rec["definition"] = {"criteria": c, "goal_basis": goal_basis,
                         "sub_national": sub_national, "n_met": n_true,
                         "n_unknown": n_unknown}
    if klass == "attack":
        if (perp.get("type") == "criminal_group" or group_cat == "criminal") and goal is not True:
            rules.append("criminal_perpetrator")
            return exclude("criminal violence (no political, religious or social goal "
                           "stated); a state designation of the group does not by itself "
                           "make an act terrorism under the working definition")
        party = kb.get(link["group_id"]) if (kb is not None and link) else None
        if party is not None and party.conflict_party and \
                crit.get("in_armed_conflict_zone") is not False and \
                crit.get("civilians_or_noncombatants_targeted") is not True:
            rules.append("conflict_party_warfare")
            return exclude(f"warfare: {party.name} is a party to an ongoing armed conflict and "
                           "the text does not say civilians were deliberately targeted")
        insurgent = (rec["target_type"] == "security_forces" and
                     crit.get("in_armed_conflict_zone") is True and civ is not True)
        if insurgent:
            rules.append("insurgency_doubt")
            rec["definition"]["met"] = None
            rec["definition"]["basis"] = ("attack on security forces within an armed conflict: "
                                          "doubt terrorism proper (insurgency)")
            rec["status"] = "possible"
            return _finish(rec, ind, base, rules)
        if n_true >= 2 and (sub_national or perp.get("type") == "unknown"
                            and flags.strong_terror):
            rec["definition"]["met"] = True
            rec["definition"]["basis"] = (f"{n_true} of 3 criteria met; perpetrator "
                                          + ("sub-national" if sub_national else
                                             "unknown, described as terrorism in the text"))
        elif n_true + n_unknown >= 2 and not _terror_indication(flags, link, goal, audience,
                                                                 rec, perp):
            rules.append("no_terror_indication")
            return exclude("violence with no indication of terrorism in the reporting (no "
                           "terrorism language, group, goal or intimidation; typical of crime "
                           "or an unknown motive)")
        elif n_true + n_unknown >= 2:
            rules.append("criteria_unknown")
            rec["definition"]["met"] = None
            rec["definition"]["basis"] = "criteria not established by the text (possible)"
            rec["status"] = "possible"
            return _finish(rec, ind, base, rules)
        else:
            rules.append("criteria_not_met")
            return exclude("fewer than 2 of the 3 additional criteria are met")
    else:  # plot / arrest / prosecution / threat need a stated terrorism nexus
        nexus = flags.strong_terror or (link or {}).get("kind") == "link" or goal is True
        if klass == "threat" and not nexus:
            # a threat of violence against the public (bomb-threat e-mails to stations,
            # schools) is aimed at a wider audience by nature; private threats are not
            nexus = audience is True or rec["target_type"] in CIVILIAN_TARGETS | {
                "transport", "energy_infrastructure", "government"}
        if not nexus:
            rules.append("no_terror_nexus")
            return exclude(f"{klass}: no terrorism nexus stated (crime / other)")
        rec["definition"]["met"] = True
        rec["definition"]["basis"] = ("terrorism named in the reporting" if flags.strong_terror
                                      else "linked group / stated motive")

    # ---------------------------------------------------------- status
    rec["status"] = "confirmed" if ind["n"] >= 2 else "reported"
    return _finish(rec, ind, base, rules)


INDICATIVE_TARGETS = {"religious", "security_forces", "government", "diplomatic",
                      "western_interests", "journalists"}
INDICATIVE_ATTACKS = {"bombing_ied", "vbied", "suicide", "vehicle_ramming", "kidnapping",
                      "assassination"}


def _terror_indication(flags, link, goal, audience, rec: dict, perp: dict) -> bool:
    """Is there ANY sign of terrorism (needed for 'possible')? Terrorism vocabulary, a group
    (even a lead), an organised non-state perpetrator, a stated goal or intimidation, an
    indicative target or modus operandi."""
    return bool(flags.strong_terror or link or goal is True or audience is True
                or perp.get("type") == "non_state_group"
                or rec.get("target_type") in INDICATIVE_TARGETS
                or rec.get("attack_type") in INDICATIVE_ATTACKS)


def _finish(rec: dict, ind: dict, base: dict | None, rules: list[str]) -> dict:
    reasons = (base or {}).get("reasons") or []
    place_conflict = any(str(r).startswith(("place:", "country:")) for r in reasons)
    if rec["status"] == "confirmed":
        if place_conflict:
            rules.append("location_contradiction")
            rec["status"] = "reported"
            rec["status_basis"] = ("independent outlets agree, but the article verification "
                                   "found a location contradiction")
        elif rec.get("follow_up") and not rec.get("occurred_at"):
            rec["status_basis"] = f"{ind['n']} independent outlets (follow-up coverage)"
        else:
            rec["status_basis"] = (f"{ind['n']} independent outlets: "
                                   + ", ".join(ind["outlets"][:6]))
    elif rec["status"] == "reported":
        rec["status_basis"] = ("single origin: " + (", ".join(ind["outlets"]) or "no counted "
                                                    "outlet") +
                               (" (Telegram only)" if ind.get("telegram_only") else "")
                               + " -- reported, unconfirmed")
    elif rec["status"] in ("possible", "context"):
        rec["status_basis"] = rec.get("definition", {}).get("basis")
    rec["confidence"] = _confidence(rec)
    return rec


def _confidence(rec: dict) -> str:
    """Categorical, rule-based (no invented decimals): high = confirmed with a verified
    location at city precision or better; medium = confirmed, or reported with a verified
    attribution; low = everything else."""
    if rec["status"] == "confirmed" and rec.get("precision") in ("point", "city"):
        return "high"
    if rec["status"] == "confirmed" or (rec["status"] == "reported" and
                                        (rec.get("attribution") or {}).get("quote")):
        return "medium"
    return "low"
