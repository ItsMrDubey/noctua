"""Actor lexicon (osint.enrich.actors) over config/actors.yaml + terror groups + countries."""
from types import SimpleNamespace

import pytest

from osint.enrich.actors import ActorLexicon, LexiconError, derive, resolve_actors


@pytest.fixture(scope="module")
def lex():
    return ActorLexicon.load()


@pytest.mark.parametrize(("text", "country"), [
    ("Russian forces pushed west", "RU"), ("the Ukrainian army said", "UA"),
    ("Ukraine army units", "UA"), ("the IDF said", "IL"), ("U.S. forces struck", "US"),
    ("Iranian drones hit Kyiv", "IR"), ("Norwegian army exercise", "NO"),
    ("Russian shelling of Kyiv", "RU"), ("Houthis attack a ship", "YE"),
    ("Hezbollah fires rockets", "LB")])
def test_generated_hand_written_and_group_phrases(lex, text, country):
    hits = lex.find(text)
    assert hits and hits[0].payload.country == country


def test_whole_words_only(tmp_path):
    p = tmp_path / "actors.yaml"
    p.write_text('entries:\n  - match: ["Iran"]\n    country: "IR"\n'
                 '    class: state_government\nconflict_categories: [armed_conflict]\n',
                 encoding="utf-8")
    lx = ActorLexicon.load(p, groups_path=tmp_path / "none.yaml")
    assert lx.find("Iranian officials") == []
    assert lx.find("Iran says")[0].payload.country == "IR"


def test_longest_match_wins(lex):
    hits = lex.find("Military Forces of Ukraine (2019-) and Wagner Group")
    assert [(h.payload.country, h.payload.klass) for h in hits] == [
        ("UA", "state_military"), ("RU", "non_state_armed")]
    assert hits[0].text == "Military Forces of Ukraine"


@pytest.mark.parametrize(("side", "country", "klass"), [
    ("Military Forces of Sudan (2019-)", "SD", "state_military"),
    ("Rapid Support Forces", "SD", "non_state_armed"),
    ("Police Forces of Kenya (2022-)", "KE", "state_police"),
    ("Protesters (Kenya)", "KE", "protesters"),
    ("Civilians (Sudan)", "SD", "civilians"),
    ("Government of Russia (Soviet Union)", "RU", "state_government"),
    ("Government of Afghanistan", "AF", "state_government"),
    ("Hamas", "PS", "non_state_armed"), ("Al-Shabaab", "SO", "non_state_armed"),
    ("IRGC", "IR", "state_military")])
def test_acled_and_ucdp_side_names(lex, side, country, klass):
    e = lex.lookup(side)
    assert e is not None and (e.country, e.klass) == (country, klass)


def test_unknown_side_names_resolve_to_none(lex):
    assert lex.lookup("Civilians") is None and lex.lookup("IS") is None
    assert lex.lookup("Georgia police") is None  # no name forms for GE (US state)


def test_null_country_entries_block_shorter_matches(lex):
    [hit] = lex.find("UN peacekeepers came under fire")
    assert hit.payload.country is None


@pytest.mark.parametrize("body", [
    'entries:\n  - match: ["X army"]\n    country: "ZZ"\n    class: state_military\n',
    'entries:\n  - match: ["X army"]\n    country: NO\n    class: state_military\n',
    'entries:\n  - match: ["X army"]\n    country: "RU"\n    class: bogus\n',
    'entries:\n  - match: []\n    country: "RU"\n    class: state_military\n',
    'terror_group_homes:\n  nope: "RU"\n',
    "",
    "entries: [unclosed\n"])
def test_a_bad_lexicon_is_fatal(tmp_path, body):
    p = tmp_path / "actors.yaml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(LexiconError):
        ActorLexicon.load(p, groups_path=tmp_path / "none.yaml")


def test_a_missing_lexicon_is_fatal(tmp_path):
    with pytest.raises(LexiconError):
        ActorLexicon.load(tmp_path / "missing.yaml")


def _doc(title, snippet=""):
    return SimpleNamespace(title=title, snippet=snippet)


def test_resolution_order_sources_and_dedupe(lex):
    ev = {"id": "01NEWS", "country": "UA", "actors": [
        {"name": "Russia", "role": "source", "type": "RUS", "country": "RUS"},  # legacy row
        {"name": "Ukraine", "role": "target", "type": "UKR", "country": "UA",
         "cameo": "UKR", "source": "gdelt"},
        {"name": "Stale", "role": "mentioned", "country": "FR", "source": "rule"}]}
    docs = [_doc("Russian shelling of Kyiv kills 3", "Russian shelling continued"),
            _doc("Kyiv under fire")]
    out = resolve_actors(ev, docs, lex,
                         [{"name": "Israel Defense Forces", "role": "perpetrator",
                           "type": None}, {"name": "Unknown gunmen", "role": "other"}])
    assert [(a["name"], a["country"], a["source"]) for a in out] == [
        ("Russia", "RU", "gdelt"), ("Ukraine", "UA", "gdelt"),
        ("Russian forces", "RU", "rule"), ("Israel Defense Forces", "IL", "llm"),
        ("Unknown gunmen", None, "llm")]
    assert out[0]["cameo"] == "RUS" and out[2]["role"] == "mentioned"
    assert out[2]["quote"] == "Russian shelling"
    assert resolve_actors({**ev, "actors": out}, docs, lex, []) == out[:3]  # idempotent


def test_acled_and_ucdp_actors_use_side_names_not_documents(lex):
    ev = {"id": "ucdp-1", "actors": [
        {"name": "Government of Afghanistan", "role": "side_a", "type": "", "country": None},
        {"name": "Civilians", "role": "side_b", "type": "", "country": None}]}
    out = resolve_actors(ev, [_doc("Government of Afghanistan - Civilians")], lex)
    assert [(a["country"], a["source"]) for a in out] == [("AF", "ucdp"), (None, "ucdp")]


def test_derive():
    acts = [{"name": "a", "country": "RU"}, {"name": "b", "country": None},
            {"name": "c", "country": "UA"}, {"name": "d", "country": "RU"}]
    assert derive(acts, "UA") == {"actor_countries": ["RU", "UA"], "foreign_involved": True,
                                  "attributed": True}
    assert derive(acts[:2], "ru")["foreign_involved"] is False
    assert derive([], "UA") == {"actor_countries": [], "foreign_involved": False,
                                "attributed": False}
    assert derive(acts, None)["foreign_involved"] is False  # no location: not "foreign"


# ---- review fix: generic demonym nouns only for conflict categories; country names inside
# longer place names ("New Mexico police") are not the country


def _rule_countries(lex, text, category):
    ev = {"id": "01NEWS", "category": category, "actors": []}
    return [a["country"] for a in resolve_actors(ev, [_doc(text)], lex) if a["source"] == "rule"]


def test_a_country_name_inside_a_longer_place_name_is_not_the_country(lex):
    assert all(h.payload.country != "MX" for h in lex.find("New Mexico police arrest suspect"))
    assert _rule_countries(lex, "New Mexico police arrest suspect", "civil_unrest") == []
    assert lex.find("Mexico police arrest suspect")[0].payload.country == "MX"
    assert lex.find("Mexican police arrest suspect")[0].payload.country == "MX"


def test_the_longest_place_name_still_wins(lex):
    hits = lex.find("Papua New Guinea army deployed")
    assert [h.payload.country for h in hits] == ["PG"]
    assert _rule_countries(lex, "Papua New Guinea army deployed", "political") == ["PG"]


@pytest.mark.parametrize(("text", "category", "expected"), [
    ("Indian attack collapses on day three", "other", []),
    ("Indian attack collapses on day three", "economic", []),
    ("Indian attack collapses on day three", None, []),
    ("Indian attack collapses on day three", "armed_conflict", ["IN"]),
    ("Chinese strike at Foxconn plant", "economic", []),
    ("Israeli strike kills 5 in Gaza", "armed_conflict", ["IL"]),
    ("Israeli jets over Beirut", "air_activity", ["IL"]),
    ("Iranian raid on a tanker", "maritime_security", ["IR"]),
    ("Russian shelling of Kyiv", "other", ["RU"]),
    ("Russian shelling of Kyiv", "economic", ["RU"]),
    ("Russian shelling of Kyiv", None, ["RU"]),
    ("Israeli forces entered Jenin", "other", ["IL"]),
    ("Israeli forces entered Jenin", "political", ["IL"])])
def test_generic_demonym_nouns_count_only_for_conflict_events(lex, text, category, expected):
    assert _rule_countries(lex, text, category) == expected


def test_conflict_only_marks_only_generic_demonym_nouns(lex):
    def flag(text):
        [hit] = lex.find(text)
        return hit.payload.conflict_only

    assert flag("Indian attack") and flag("Chinese strikes") and flag("Israeli jets")
    assert not flag("Russian shelling") and not flag("Israeli forces")
    assert not flag("Iranian drones") and not flag("Russian missiles")


def test_actor_entry_positional_fields_still_work():
    from osint.enrich.actors import ActorEntry

    e = ActorEntry("Russian forces", "RU", "state_military")
    assert (e.name, e.country, e.klass, e.conflict_only) == (
        "Russian forces", "RU", "state_military", False)


_ENTRY = 'entries:\n  - match: ["X army"]\n    country: "RU"\n    class: state_military\n'


@pytest.mark.parametrize("cats", [
    "", "conflict_categories: []\n", "conflict_categories:\n",
    "conflict_categories: [bogus_category]\n", "conflict_categories: armed_conflict\n",
    "conflict_categories: [[armed_conflict]]\n",
    "conflict_categories: [armed_conflict]\ngenerated:\n  name_left_block: new\n"])
def test_conflict_categories_must_be_known_event_categories(tmp_path, cats):
    p = tmp_path / "actors.yaml"
    p.write_text(_ENTRY + cats, encoding="utf-8")
    with pytest.raises(LexiconError):
        ActorLexicon.load(p, groups_path=tmp_path / "none.yaml")


def test_the_shipped_conflict_categories(lex):
    assert lex.conflict_categories == frozenset(
        {"armed_conflict", "maritime_security", "air_activity"})
