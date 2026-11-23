"""Regression suite derived from the ten defects fixed on 2026-11-23 (one block per
lesson). Each test pins a behaviour that must never regress."""

from datetime import UTC, datetime

import numpy as np

from osint.terror import classify, dedupe, indicators, outlets, text
from osint.terror.intel import section_terror
from osint.terror.store import TerrorStore
from tests.terror.conftest import doc, event, geocoder, terr

NOW = datetime(2026, 11, 23, 12, tzinfo=UTC)
GEO = geocoder({"Peshawar": (34.008, 71.578, "PK", "city"), "Kyiv": (50.45, 30.52, "UA", "city"),
                "Frankfurt": (50.11, 8.68, "DE", "city"), "Kim": (21.4, 85.0, "IN", "city"),
                "Engels": (51.5, 46.1, "RU", "city")})


# ---------------------------------------------------------------- 1. denials are not evidence

def test_denial_headlines_are_detected():
    for t in [("NOTE: Contrary to reports, there are no missile launches identified from "
              "Khuzestan / Ahvaz at this time."),
              "Police: no attack was identified at the station",
              "Explosion reports were a false alarm, officials say",
              "Bomb threat at the airport was a hoax",
              "Officials deny reports of an explosion at the refinery",
              "МВД: информация о взрыве не соответствует действительности",
              "الأمن: لا صحة لأنباء عن انفجار في المطار"]:
        assert text.is_denial(t), t


def test_denial_of_involvement_is_not_a_denial_of_the_incident():
    for t in ["Lithuania denies involvement in attack on Kaliningrad region",
              "Group denies responsibility for the bombing that killed 12",
              "No casualties reported after drone attack"]:
        assert not text.is_denial(t), t


def test_denial_headline_is_excluded_without_llm():
    ev = event(title="NOTE: Contrary to reports, there are no missile launches identified "
                     "from Khuzestan / Ahvaz at this time.")
    rec = classify.classify(ev, None, None, now=NOW)
    assert rec["status"] == "excluded" and "denial" in rec["excluded_reason"]


def test_extraction_saying_it_did_not_happen_excludes():
    rec = classify.classify(event(), None, terr(occurred="no",
                                                negation_quote="no explosion took place"),
                            now=NOW)
    assert rec["status"] == "excluded" and "did not happen" in rec["excluded_reason"]


def test_denial_of_involvement_keeps_the_attack():
    ev = event(title="Group denies involvement in Peshawar market bombing",
               docs=[doc("https://www.dawn.com/a", "Group denies involvement in Peshawar "
                         "market bombing"), doc("https://www.bbc.com/b", "Peshawar market "
                                                "bombing kills 12")])
    rec = classify.classify(ev, None, terr(), geocode=GEO, now=NOW)
    assert rec["status"] == "confirmed" and rec["klass"] == "attack"


def test_unsupported_extraction_verdict_excludes():
    base = {"status": "done", "verdict": "unsupported", "result": {"is_real_event": False}}
    rec = classify.classify(event(), base, None, now=NOW)
    assert rec["status"] == "excluded" and "no concrete event" in rec["excluded_reason"]


# ---------------------------------------------------------------- 2. single report != confirmed

def test_single_outlet_is_reported_unconfirmed():
    rec = classify.classify(event(), None, terr(), geocode=GEO, now=NOW)
    assert rec["status"] == "reported"
    assert "unconfirmed" in rec["status_basis"]


def test_many_telegram_channels_are_one_outlet():
    docs = [doc(f"https://t.me/channel{i}/{i}", f"Explosion at market {i}", source=f"telegram:c{i}")
            for i in range(5)]
    ind = outlets.independent_outlets(docs)
    assert ind["outlets"] == ["t.me"] and ind["telegram_only"]
    rec = classify.classify(event(docs=docs), None, terr(), geocode=GEO, now=NOW)
    assert rec["status"] == "reported"


def test_two_independent_outlets_confirm():
    docs = [doc("https://www.dawn.com/1", "Suicide bomber kills 12 at Peshawar market"),
            doc("https://www.reuters.com/2", "Blast at crowded Pakistan bazaar leaves 12 dead")]
    rec = classify.classify(event(docs=docs), None, terr(), geocode=GEO, now=NOW)
    assert rec["status"] == "confirmed" and rec["outlets"]["n"] == 2


# ---------------------------------------------------------------- 3. no self-corroboration

def test_same_organisation_feeds_do_not_corroborate():
    docs = [doc("https://www.france24.com/en/x", "Bomb kills 12 in Peshawar"),
            doc("https://www.france24.com/ar/y", "مقتل 12 بتفجير في بيشاور", lang="ar"),
            doc("https://www.aljazeera.net/z", "انفجار في بيشاور", lang="ar"),
            doc("https://www.aljazeera.com/w", "Peshawar blast")]
    assert outlets.independent_outlets(docs)["n"] == 2  # france24 + aljazeera


def test_reposts_of_one_story_are_one_origin():
    title = "Suicide bomber kills 12 at crowded market in Peshawar police say"
    docs = [doc("https://www.dawn.com/1", title, published="2026-11-20T10:00:00+00:00"),
            doc("https://www.somesite.pk/2", title, published="2026-11-20T10:30:00+00:00"),
            doc("https://t.me/news/3", title, source="telegram:news",
                published="2026-11-20T11:00:00+00:00")]
    ind = outlets.independent_outlets(docs)
    assert ind["n"] == 1 and ind["outlets"] == ["dawn.com"]


def test_gdelt_and_derived_sources_never_count():
    docs = [doc("https://www.dawn.com/1", "a"), doc("https://x.org/2", "b", source="gdelt")]
    assert outlets.independent_outlets(docs)["n"] == 1


# ---------------------------------------------------------------- 4. geocoding traps

def test_capital_metonyms_are_detected():
    assert text.is_metonym("Kyiv", "Kyiv named the suspects in the Frankfurt attack")
    assert text.is_metonym("Washington", "Washington says the group was behind it")
    assert text.is_metonym("Tehran", "Tehran accuses Israel of the bombing")
    assert not text.is_metonym("Kyiv", "A bomb exploded in Kyiv on Monday")
    assert not text.is_metonym("Peshawar", "Peshawar police said the bomber acted alone")


def test_metonym_never_becomes_the_attack_site():
    t = terr(where={"place_name": "Kyiv", "country": "UA", "precision": "city",
                    "quote": "Kyiv named the suspects"})
    loc = classify.resolve_location(event(), t, None, GEO)
    assert loc["lat"] is None and "metonym" in loc["basis"]


def test_place_without_verbatim_quote_is_country_precision_only():
    t = terr(where={"place_name": "Kim", "country": "IN", "precision": "city", "quote": None})
    loc = classify.resolve_location(event(), t, None, GEO)
    assert loc["lat"] is None and loc["precision"] == "country" and loc["country"] == "IN"


def test_geocode_in_another_country_is_not_trusted():
    # a former / foreign name resolving outside the stated country (Pokrovsk/Engels trap)
    t = terr(where={"place_name": "Engels", "country": "UA", "precision": "city",
                    "quote": "near Engels"})
    loc = classify.resolve_location(event(), t, None, GEO)
    assert loc["lat"] is None and loc["precision"] == "country" and loc["country"] == "UA"


def test_location_from_incident_description_has_basis_and_precision():
    rec = classify.classify(event(), None, terr(), geocode=GEO, now=NOW)
    assert rec["precision"] == "city" and rec["lat"] == 34.008
    assert "at a market in Peshawar" in rec["location"]["basis"]


# ---------------------------------------------------------------- 5. event time != processing time

def test_stated_occurrence_time_is_used():
    rec = classify.classify(event(), None, terr(when="2026-11-19", when_quote="on Thursday, 19 "
                                                "November"), geocode=GEO, now=NOW)
    assert rec["occurred_at"].startswith("2026-11-19") and rec["sort_time"] == rec["occurred_at"]


def test_old_attack_in_new_article_is_follow_up_not_new():
    rec = classify.classify(event(), None, terr(when="2026-07-25", when_quote="on 25 July"),
                            geocode=GEO, now=NOW)
    assert rec["follow_up"] is True and "follow_up_coverage" in rec["rules"]
    tempo = indicators.news_tempo([rec], NOW, coverage_days=400)
    assert tempo["observed"] == 0  # never counted as a new incident


def test_unstated_time_uses_first_report_never_processing_time():
    rec = classify.classify(event(), None, terr(), geocode=GEO, now=NOW)
    assert rec["occurred_at"] is None and rec["sort_time"] == "2026-11-20T10:00:00+00:00"


def test_when_after_first_report_is_ignored():
    rec = classify.classify(event(), None, terr(when="2026-11-22", when_quote="on Sunday"),
                            geocode=GEO, now=NOW)
    assert rec["occurred_at"] is None and "when_after_first_report_ignored" in rec["rules"]


# ---------------------------------------------------------------- 6. cross-language duplicates

def _inc(iid, lat, lon, cc="PK", t="2026-11-20T10:00:00+00:00", n=1):
    return {"id": iid, "event_id": iid, "klass": "attack", "status": "reported", "country": cc,
            "lat": lat, "lon": lon, "first_reported_at": t, "occurred_at": None,
            "attack_type": "suicide", "group": None, "outlets": {"n": n},
            "casualties": {"killed": {"low": 12, "high": 12}}}


def test_same_attack_in_two_languages_merges_to_one():
    v = np.ones(8, dtype=np.float32) / np.sqrt(8)
    a, b = _inc("A", 34.0, 71.5, n=2), _inc("B", 34.01, 71.55)
    pairs = dedupe.merge([a, b], {"A": v, "B": v})
    assert pairs and pairs[0][0] == "B" and pairs[0][1] == "A"


def test_different_incidents_do_not_merge():
    v1 = np.eye(8, dtype=np.float32)[0]
    v2 = np.eye(8, dtype=np.float32)[1]
    a, b = _inc("A", 34.0, 71.5), _inc("B", 34.0, 71.5)
    b["casualties"] = {"killed": {"low": 3, "high": 3}}
    assert dedupe.merge([a, b], {"A": v1, "B": v2}) == []


# ---------------------------------------------------------------- 7. real distances

def test_dedupe_uses_real_distance_kyiv_is_not_frankfurt():
    v = np.ones(8, dtype=np.float32) / np.sqrt(8)
    a, b = _inc("A", 50.45, 30.52, cc=None), _inc("B", 50.11, 8.68, cc=None)
    ok, why = dedupe.same_incident(a, b, v, v)
    assert not ok and "km apart" in why


def test_nearby_section_filters_by_true_distance(tmp_path):
    from types import SimpleNamespace

    store = TerrorStore(tmp_path / "terror.sqlite")
    frankfurt = {**_inc("ti-F", 50.11, 8.68, cc="DE"), "title": "Frankfurt incident",
                 "sort_time": "2026-11-20T10:00:00+00:00", "precision": "city"}
    kyiv = {**_inc("ti-K", 50.45, 30.52, cc="UA"), "title": "Kyiv incident",
            "sort_time": "2026-11-20T10:00:00+00:00", "precision": "city"}
    store.replace_incidents([frankfurt, kyiv], NOW)
    rt = SimpleNamespace(terror_store=store, settings=SimpleNamespace(data_dir=tmp_path))
    out = section_terror(rt, {"kind": "place", "lat": 50.11, "lon": 8.68}, NOW)
    titles = [i["title"] for i in out["nearby"]["items"]]
    assert titles == ["Frankfurt incident"]  # Kyiv is ~1,560 km away, outside 300 km


# ---------------------------------------------------------------- 8. name-only matches are leads

def test_group_name_only_in_extracted_field_is_a_lead(kb):
    link = kb.link(perpetrator="Islamic State", quotes=[], level="suspected")
    assert link["kind"] == "lead" and link["confidence"] <= 0.3


def test_alias_in_verbatim_quote_is_a_link_with_basis(kb):
    link = kb.link(perpetrator="ISKP", quotes=["ISKP claimed responsibility for the attack"],
                   level="claimed")
    assert link["group_id"] == "iskp" and link["kind"] == "link" and "verbatim" in link["basis"]


def test_ambiguous_short_name_never_matches_free_text(kb):
    assert kb.find("IS this the end of the war?") == []
    assert kb.link(perpetrator=None, quotes=["IS fighters attacked"], level="claimed") is None


def test_designations_match_by_exact_name_only(kb):
    assert kb.match_designation(["NORTH STAR"]) == (None, None)
    assert kb.match_designation(["Islamic State of Iraq"]) == (None, None)  # not an alias
    gid, _ = kb.match_designation(["Islamic State's Khorasan Province", "ISIS-K"])
    assert gid == "iskp"


def test_state_umbrella_term_is_not_attributed_to_one_group(kb):
    assert kb.find("killed by Fitna al-Hindustan") == []


# ---------------------------------------------------------------- 9. baselines

def test_short_news_history_says_no_baseline_yet():
    rec = classify.classify(event(), None, terr(), geocode=GEO, now=NOW)
    t = indicators.news_tempo([rec], NOW, coverage_days=4)
    assert t["available"] is False and t["verdict"] == "no baseline yet"


def test_chronic_background_is_within_normal_variation():
    rows = [{"month": m, "events": 10, "deaths_best": 20, "group_id": "isis",
             "category": "jihadist"} for m in indicators.month_range("2024-01", "2026-10")]
    t = indicators.ucdp_tempo(rows, ("2024-01", "2026-10"))
    assert t["verdict"] == "within normal variation" and t["expected"] == 30.0


def test_spike_against_baseline_is_flagged_with_test():
    rows = [{"month": m, "events": 10 if m < "2026-08" else 40, "deaths_best": 0,
             "group_id": "isis", "category": "jihadist"}
            for m in indicators.month_range("2024-01", "2026-10")]
    t = indicators.ucdp_tempo(rows, ("2024-01", "2026-10"))
    assert t["verdict"] == "above baseline" and t["p_above"] < 0.05


# ---------------------------------------------------------------- 10. no fabricated precision

def test_casualty_disagreement_is_a_range_with_sources():
    ev = event(docs=[doc("https://www.dawn.com/1", "Suicide bomber kills 12 at market"),
                     doc("https://www.bbc.com/2", "At least 15 killed in Peshawar blast")])
    rec = classify.classify(ev, None, terr(), geocode=GEO, now=NOW)
    k = rec["casualties"]["killed"]
    assert (k["low"], k["high"], k["disagreement"]) == (12, 15, True)
    assert {s["source"].split(" ")[0] for s in rec["casualties"]["sources"]} >= {"terror",
                                                                                 "headline"}


def test_missing_figures_are_not_zero_deaths():
    ev = event(title="Suicide bombing at a market in Peshawar")
    a = classify.classify(ev, None, terr(casualties={"killed": None, "injured": None,
                                                     "quote": None}),
                          geocode=GEO, now=NOW)
    a["sort_time"] = "2026-11-22T00:00:00+00:00"
    leth = indicators.lethality([a], NOW)
    assert leth["incidents"] == 1 and leth["with_figures"] == 0 and leth["killed_low_sum"] == 0


def test_posture_band_widens_when_components_are_missing():
    rates = {"available": True, "mean_deaths": 100.0,
             "years": [{"year": str(y), "events": 10, "deaths": 100} for y in range(2021, 2026)]}
    full = indicators.posture(levels=[{"authority": "MI5", "level": "SEVERE", "rank": 4,
                                       "scale_max": 5, "scope": "national"}],
                              advisory={"level": 2, "terrorism": True}, rates=rates,
                              tempo={"available": False}, recent_incidents=1)
    thin = indicators.posture(levels=[], advisory=None, rates=rates,
                              tempo={"available": False}, recent_incidents=None)
    assert thin["coverage"] < full["coverage"]
    assert (thin["band"][1] - thin["band"][0]) > (full["band"][1] - full["band"][0])
    assert "not a prediction" in full["label"]
