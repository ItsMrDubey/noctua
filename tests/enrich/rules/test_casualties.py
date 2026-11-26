import pytest

from osint.enrich.rules import RuleSet
from osint.enrich.rules.base import ExtractDoc
from osint.enrich.rules.casualties import casualties


@pytest.fixture(scope="module")
def rules():
    return RuleSet.load().casualties


@pytest.mark.parametrize(("text", "field", "value"), [
    ("Missile strike kills 12 in Kharkiv", "killed", 12),
    ("At least 12 people were killed in the attack", "killed", 12),
    ("12 dead after shelling", "killed", 12),
    ("Three people were killed in Kharkiv", "killed", 3),
    ("Death toll rises to 17", "killed", 17),
    ("1,200 killed in floods", "killed", 1200),
    ("killing 3 and injuring 12", "injured", 12),
    ("Strike on Kharkiv, 3 injured", "injured", 3),
    ("Frappe sur Kharkiv : 12 morts et 30 blessés", "killed", 12),
    ("Frappe sur Kharkiv : 12 morts et 30 blessés", "injured", 30),
    ("Ataque deja 7 muertos y 15 heridos", "killed", 7),
    ("Ataque deja 7 muertos y 15 heridos", "injured", 15),
    ("مقتل 12 شخصا في غارة إسرائيلية", "killed", 12),
    ("قتل 12", "killed", 12),
    ("مقتل ١٢ شخصا", "killed", 12),
    ("مقتل ثلاثة أشخاص وإصابة خمسة", "killed", 3),
    ("مقتل ثلاثة أشخاص وإصابة خمسة", "injured", 5),
    ("В результате удара погибли 12 человек, ранены 30", "killed", 12),
    ("В результате удара погибли 12 человек, ранены 30", "injured", 30),
    ("погибли пять человек", "killed", 5),
    ("Унаслідок атаки загинули 5 людей, ще 12 поранені", "killed", 5),
    ("Унаслідок атаки загинули 5 людей, ще 12 поранені", "injured", 12),
    ("загинули п’ять людей", "killed", 5),
    ("twenty-one killed", "killed", None),
    ("thirty two killed", "killed", None),
    ("forty-five killed in blast", "killed", None),
    ("vingt-et-un morts", "killed", None),
    ("двадцать один погибли", "killed", None),
    ("двадцять один загинули", "killed", None),
    ("veinte y uno muertos", "killed", None),
    ("veintiún muertos", "killed", None),
    ("treinta y uno muertos", "killed", None),
    ("twenty killed", "killed", 20),
    ("one killed", "killed", 1),
    ("1 200 killed", "killed", 1200),
    ("1 200 morts", "killed", 1200),
    ("погибли 1 200 человек", "killed", 1200),
    ("1 200 morts", "killed", 1200),
    ("1 200 morts", "killed", 1200),
    ("12 killed in 2023 attack", "killed", 12),
    ("In 2023 12 killed", "killed", 12),
    # documented ambiguity: "killed 12 300 injured" reads as 12300 (strict 3-digit groups)
    ("killed 12 300 injured", "killed", 12300),
    ("A 12-year-old was killed", "killed", None),
    ("Nobody was hurt", "injured", None)])
def test_patterns_per_language(rules, text, field, value):
    hit = rules.doc_value(field, text)
    assert (hit[0] if hit else None) == value


@pytest.mark.parametrize(("text", "field", "value"), [
    # a number followed by a unit is a distance / weight / share / age, never a count: the
    # space-grouped reading is rejected and only the leading group can stand
    ("Strike killed 5 200 km north of Kyiv", "killed", 5),
    ("Shelling injures 12 300 metres from the front", "injured", 12),
    ("Strike killed 200 km north of Kyiv", "killed", None),
    ("Strike killed 200km north of Kyiv", "killed", None),
    ("Blast injures 40 kg of explosives", "injured", None),
    ("killed 3 miles away", "killed", None),
    ("killed 50 percent of the herd", "killed", None),
    ("killed 50 % of the herd", "killed", None),
    ("killed 20 years ago", "killed", None),
    ("убиты 5 км от города", "killed", None),
    ("загинули 300 метрів від лінії фронту", "killed", None),
    ("погибли 1 200 метров", "killed", 1),
    ("tué 3 kilomètres plus loin", "killed", None),
    ("mató a 10 kilómetros", "killed", None),
    ("مقتل 5 كم شمال المدينة", "killed", None),
    # counts still read
    ("Strike killed 5 people near Kyiv", "killed", 5),
    ("1 200 killed", "killed", 1200),
    ("12 killed 5 km north of Kyiv", "killed", 12),
    ("Missile kills 7 in Kharkiv", "killed", 7)])
def test_numbers_followed_by_a_unit_are_not_counts(rules, text, field, value):
    hit = rules.doc_value(field, text)
    assert (hit[0] if hit else None) == value


def test_units_are_configured():
    from osint.enrich.rules.casualties import CasualtyRules
    cfg = {"killed": {"en": [r"\bkilled\s+{n}\b"]}, "units": ["furlongs"]}
    r = CasualtyRules(cfg)
    assert r.doc_value("killed", "killed 3 furlongs away") is None
    assert r.doc_value("killed", "killed 3 km away")[0] == 3  # only the configured units


def test_median_over_distinct_domains_ignoring_silent_documents(rules):
    docs = [ExtractDoc("a", "Strike kills 10", url="https://a.example/1", published_at="1"),
            ExtractDoc("b", "12 killed in strike", url="https://b.example/1", published_at="2"),
            ExtractDoc("c1", "15 dead", url="https://c.example/1", published_at="3"),
            ExtractDoc("c2", "Death toll rises to 17", url="https://www.c.example/2",
                       published_at="4"),
            ExtractDoc("d", "No casualties reported", url="https://d.example/1",
                       published_at="5")]
    [v] = casualties({}, docs, rules=rules)
    # domains: a=10, b=12, c=max(15, 17)=17 -> lower median 12
    assert (v.field, v.value, v.doc_id, v.quote) == ("casualties.killed", "12", "b",
                                                     "12 killed")


def test_no_numbers_no_values(rules):
    assert casualties({}, [ExtractDoc("a", "Explosion reported in Kharkiv")],
                      rules=rules) == []


def test_ruleset_includes_casualties():
    rs = RuleSet.load()
    assert "casualties" in [name for name, _ in rs.extractors()]
    vals, _ = rs.run({"id": "E1"}, [ExtractDoc("a", "Drone strike kills 4",
                                               url="https://a.example/1")])
    assert ("casualties.killed", "4") in {(v.field, v.value) for v in vals}
