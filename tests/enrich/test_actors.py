"""Actor lexicon (osint.enrich.actors) over config/actors.yaml + terror groups + countries."""
import pytest

from osint.enrich.actors import ActorLexicon, LexiconError


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
                 '    class: state_government\n', encoding="utf-8")
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
