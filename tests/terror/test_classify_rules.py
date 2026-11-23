"""Classification rules found while iterating on the labelled sample."""

from datetime import UTC, datetime

import numpy as np

from osint.terror import classify, dedupe
from tests.terror.conftest import doc, event, geocoder, terr

NOW = datetime(2026, 11, 23, 12, tzinfo=UTC)
GEO = geocoder({"Khar": (51.0, 0.0, "GB", "city"), "Peshawar": (34.0, 71.5, "PK", "city")})
NO_PERP = {"name": None, "type": "unknown", "quote": None}
NO_ATTR = {"level": "unknown", "by": None, "channel": None, "quote": None}
UNKNOWN = {"political_religious_social_goal": None, "intimidate_wider_audience": None,
           "civilians_or_noncombatants_targeted": None, "in_armed_conflict_zone": None}


def test_place_without_any_country_is_not_placed():
    t = terr(where={"place_name": "Khar", "admin1": "Bajaur", "country": None,
                    "precision": "city", "quote": "Khar, Bajaur"})
    loc = classify.resolve_location(event(), t, None, GEO)
    assert loc["lat"] is None and "without a country" in loc["basis"]


def test_shooting_without_any_terrorism_indication_is_excluded():
    t = terr(attack_type="mass_shooting", target_type="civilians", perpetrator=NO_PERP,
             attribution=NO_ATTR, criteria=UNKNOWN)
    ev = event(title="3 killed and 1 wounded in shooting at Ohio party, police say")
    rec = classify.classify(ev, None, t, now=NOW)
    assert rec["status"] == "excluded" and "no_terror_indication" in rec["rules"]


def test_named_armed_group_keeps_it_possible():
    t = terr(attack_type="armed_assault", target_type="civilians", attribution=NO_ATTR,
             perpetrator={"name": "United Baloch Army", "type": "non_state_group",
                          "quote": "The United Baloch Army (UBA) cadres carried out an attack"},
             criteria=UNKNOWN)
    rec = classify.classify(event(), None, t, now=NOW)
    assert rec["status"] == "possible"
    assert rec["attribution"]["level"] == "suspected"  # named by the outlet, not claimed


def test_conflict_party_attack_without_civilian_targeting_is_warfare(kb):
    t = terr(kind="attempted_attack", target_type="energy_infrastructure",
             perpetrator={"name": "Houthis", "type": "non_state_group", "quote": None},
             attribution=NO_ATTR, criteria={**UNKNOWN, "in_armed_conflict_zone": True,
                                            "intimidate_wider_audience": True})
    rec = classify.classify(event(title="Houthis say they attacked power plant"), None, t,
                            kb=kb, now=NOW)
    assert rec["status"] == "excluded" and "conflict_party_warfare" in rec["rules"]


def test_hoax_bomb_threat_is_a_threat_item_not_a_denial():
    t = terr(kind="threat_or_hoax", occurred="no", negation_quote="It seems to be a hoax",
             perpetrator=NO_PERP, attribution=NO_ATTR, criteria=UNKNOWN,
             target_type="transport")
    ev = event(title="Police issue alert over bomb threat email; it seems to be a hoax")
    rec = classify.classify(ev, None, t, now=NOW)
    assert rec["klass"] == "threat" and rec["status"] == "reported"


def test_state_directed_plot_is_excluded():
    t = terr(kind="plot_foiled", perpetrator={"name": "Russian intelligence",
                                              "type": "state_security", "quote": None})
    rec = classify.classify(event(title="EU condemns alleged Russian plot to kill activist"),
                            None, t, now=NOW)
    assert rec["status"] == "excluded"


def test_arrest_by_security_forces_is_not_a_state_perpetrator_exclusion():
    t = terr(kind="arrest", perpetrator={"name": "police", "type": "state_security",
                                         "quote": None})
    rec = classify.classify(event(title="Police arrest explosives expert linked to five "
                                        "atentados"), None, t, now=NOW)
    assert rec["klass"] == "arrest" and rec["status"] == "reported"


def test_duplicates_merge_transitively_through_any_member():
    v1, v2 = np.zeros(4, np.float32), np.zeros(4, np.float32)
    v1[0], v2[1] = 1.0, 1.0
    v12 = (v1 + v2) / np.linalg.norm(v1 + v2)
    base = {"klass": "attack", "status": "possible", "country": "PS", "lat": 31.9, "lon": 35.2,
            "first_reported_at": "2026-11-22T10:00:00+00:00", "occurred_at": None,
            "attack_type": "unknown", "group": None, "casualties": {}}
    a = {**base, "id": "A", "event_id": "A", "outlets": {"n": 4}}
    b = {**base, "id": "B", "event_id": "B", "outlets": {"n": 2}}
    c = {**base, "id": "C", "event_id": "C", "outlets": {"n": 1}}
    pairs = dedupe.merge([a, b, c], {"A": v1, "B": v12, "C": v2})
    assert {p[0] for p in pairs} == {"B", "C"} and {p[1] for p in pairs} == {"A"}


def test_contradicting_attack_types_never_merge():
    v = np.ones(4, np.float32) / 2
    base = {"klass": "attack", "status": "reported", "country": "AF", "lat": 34.5, "lon": 69.2,
            "first_reported_at": "2026-11-22T10:00:00+00:00", "group": None, "casualties": {},
            "outlets": {"n": 1}}
    a = {**base, "id": "A", "event_id": "A", "attack_type": "suicide"}
    b = {**base, "id": "B", "event_id": "B", "attack_type": "stabbing"}
    assert dedupe.merge([a, b], {"A": v, "B": v}) == []


def test_doc_helper_counts():
    assert doc("https://x.org/1", "t")["source"] == "rss:x"
