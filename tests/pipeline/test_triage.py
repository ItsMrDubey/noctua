from datetime import UTC, datetime

import pytest

from osint.core.models import Document
from osint.enrich.gazetteer import Gazetteer
from osint.pipeline.triage import DEFAULT_CONFIG, Lexicon, Triage, detect_language
from tests.gazetteer_fixture import write_gazetteer

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def triage(tmp_path_factory):
    gz = Gazetteer.load(write_gazetteer(tmp_path_factory.mktemp("gaz")))
    return Triage.from_file(DEFAULT_CONFIG, gz)


def _doc(title, snippet="", language="", kind="article", attrs=None):
    return Document(id="doc:x", source="t", url="https://e.x/a", title=title, snippet=snippet,
                    language=language, published_at=NOW, fetched_at=NOW, kind=kind,
                    attrs=attrs or {})


@pytest.mark.parametrize(("text", "lang"), [
    ("Russian missile strike hits Kharkiv apartment block", "en"),
    ("غارة إسرائيلية على بيروت والضاحية الجنوبية", "ar"),
    ("حمله پهپادی به اصفهان و گزارش کشته شدن چند نفر", "fa"),
    ("Удар по Харькову: погибли три человека", "ru"),
    ("Масована атака дронів на Київ: є загиблі", "uk"),
    ("乌克兰称俄罗斯导弹袭击基辅", "zh"),
    ("イスラエル軍攻撃で18人死亡の報道", "ja"),
    ("צה״ל תקף בדרום לבנון", "he"),
    ("Protestas masivas en Caracas tras las elecciones de la semana", "es"),
    ("Les manifestants ont bloqué la route dans le centre de la ville", "fr"),
    ("Die Regierung hat den Notstand für das ganze Land ausgerufen", "de"),
    ("İsrail'in hava saldırısında 12 kişi öldü, ağır yaralılar var", "tr"),
    ("Apple unveils a new phone", "en"),
    ("", ""),
])
def test_detect_language(text, lang):
    assert detect_language(text) == lang


def test_football_strike_is_not_conflict(triage):
    r = triage.assess(_doc("Kane strike earns Bayern Munich win over Dortmund",
                           "Harry Kane scored in the second half as Bayern beat Dortmund "
                           "in the Bundesliga."))
    assert "armed_conflict" not in r.categories
    assert r.relevance < 0.35


def test_hunger_and_labour_strikes_are_not_conflict(triage):
    r = triage.assess(_doc("Prisoner on hunger strike for 40 days"))
    assert r.categories == [] and r.relevance == 0
    r = triage.assess(_doc("Teachers strike over pay as unions walk out"))
    assert "armed_conflict" not in r.categories


def test_cricket_story_with_conflict_words_is_penalised(triage):
    r = triage.assess(_doc("Australia attack England bowlers to win T20 thriller",
                           "Australia beat England by three wickets in Melbourne."))
    assert r.relevance < 0.35


def test_english_conflict_with_place(triage):
    r = triage.assess(_doc("Russian missile strike hits Kharkiv apartment block, Ukraine says"))
    assert r.categories[0] == "armed_conflict"
    assert r.relevance > 0.8
    assert r.language == "en"
    assert r.best_place.name == "Kharkiv" and r.best_place.precision == "city"


@pytest.mark.parametrize(("title", "category", "place"), [
    ("غارة إسرائيلية على بيروت", "armed_conflict", "Beirut"),
    ("حمله پهپادی به اصفهان", "armed_conflict", "Isfahan"),
    ("Удар по Харькову: погибли три человека", "armed_conflict", "Kharkiv"),
    ("Масована атака дронів на Київ: є загиблі", "armed_conflict", "Kyiv"),
    ("Manifestations et gaz lacrymogène à Paris", "civil_unrest", "Paris"),
    ("Protestas masivas y toque de queda en París", "civil_unrest", None),
    ("Stromausfall in München nach Brandanschlag auf Umspannwerk", "infrastructure", "Munich"),
    ("İsrail'in hava saldırısında 12 kişi öldü", "armed_conflict", None),
    ("俄罗斯导弹袭击基輔", "armed_conflict", "Kyiv"),
    ("Houthis claim attack on tanker in the Red Sea off Hodeidah", "maritime_security",
     "Al Hudaydah"),
    ("Magnitude 6.8 earthquake triggers tsunami warning", "disaster", None),
    ("Ransomware attack causes nationwide internet outage", "cyber_outage", None),
    ("Fighter jets scrambled after airspace violation", "air_activity", None),
])
def test_multilingual_positives(triage, title, category, place):
    r = triage.assess(_doc(title))
    assert category in r.categories, (title, r.scores)
    assert r.relevance >= 0.35
    if place:
        assert r.best_place is not None and r.best_place.name == place


def test_irrelevant_document(triage):
    r = triage.assess(_doc("Apple unveils new iPhone with better camera"))
    assert r.relevance == 0 and r.categories == []


def test_snippet_hits_weigh_less_than_title(triage):
    in_title = triage.score_text("Airstrike on depot", "")["armed_conflict"]
    in_snippet = triage.score_text("Depot news", "Airstrike on depot")["armed_conflict"]
    assert in_title > in_snippet > 0


def test_source_language_is_kept(triage):
    assert triage.assess(_doc("Kharkiv", language="uk")).language == "uk"


def test_gdelt_document_uses_coded_geo_and_cameo(triage):
    r = triage.assess(_doc(
        "Russia — Fight — Ukraine (190) in Kharkiv, Ukraine", "Kharkiv, Ukraine",
        kind="gdelt_event", attrs={
            "event_code": "190", "event_root_code": "19", "quad_class": 4,
            "num_articles": 8, "geo_type": 4, "geo_lat": 49.98, "geo_lon": 36.25,
            "geo_name": "Kharkiv, Kharkivs'ka Oblast', Ukraine", "geo_country": "UP"}))
    assert r.categories[0] == "armed_conflict"
    assert r.relevance >= 0.6
    assert r.best_place.precision == "city" and r.best_place.lat == 49.98
    assert r.best_place.feature == "gdelt:4"


def test_gdelt_overrides_and_country_geo(triage):
    r = triage.assess(_doc("A — Coerce — B", kind="gdelt_event", attrs={
        "event_code": "176", "event_root_code": "17", "quad_class": 4, "num_articles": 1,
        "geo_type": 1, "geo_lat": 10.0, "geo_lon": 20.0}))
    assert "cyber_outage" in r.categories
    assert r.best_place.precision == "country"
    r = triage.assess(_doc("A — Consult — B", kind="gdelt_event", attrs={
        "event_code": "040", "event_root_code": "04", "quad_class": 1, "num_articles": 1,
        "geo_type": 0, "geo_lat": None, "geo_lon": None}))
    assert r.relevance < 0.35 and r.best_place is None


def test_lexicon_prefix_phrase_and_clitics():
    lex = Lexicon()
    lex.add("a", "airstrike*")
    lex.add("b", "ракетн* удар*")
    lex.add("c", "قصف")
    lex.add("d", "空袭")
    lex.add("e", "b-52")
    lex.add("mask", "hunger strike*")
    lex.add("f", "strike")
    groups = lambda text: {lex.terms[i].group for i in lex.find(text)}
    assert groups("Airstrikes overnight") == {"a"}
    assert groups("ракетный удар по городу") == {"b"}
    assert groups("وقصف عنيف") == {"c"}
    assert groups("以色列空袭加沙") == {"d"}
    assert groups("A B-52 flew") == {"e"}
    assert groups("hunger strikes continue") == set()
    assert groups("a strike and a hunger strike") == {"f"}
    assert groups("striker") == set()


# live false positive 01M7Z9WE92V2YBSXB10NRGNSJ9: Persian "Real Madrid" folded to the rial
COURTOIS_TITLE = "رئال مادرید قرارداد تیبو کورتوا را تا ۲۰۲۸ تمدید کرد"
COURTOIS_SNIPPET = ("باشگاه رئال مادرید از توافق رسمی با تیبو کورتوا، دروازه‌بان بلژیکی خود، "
                    "برای تمدید قرارداد تا ۳۰ ژوئن ۲۰۲۸ خبر داد.")


@pytest.mark.parametrize(("title", "snippet", "url"), [
    (COURTOIS_TITLE, COURTOIS_SNIPPET, "https://www.iranintl.com/202611217282"),
    ("رافائل نادال، اسطوره تنیس به جمع مدیران رئال مادرید پیوست", "",
     "https://www.iranintl.com/202611217282"),
    ("بيريز يعلن إيرادات قياسية لريال مدريد ويكشف خطة اليويفا للبث المجاني", "",
     "https://www.aljazeera.net/x/2026/11/21/a"),
    ("Real Madrid posts record revenue as Barcelona trims its wage bill", "",
     "https://e.x/a"),
    ("Inflation data: prices rise again", "", "https://www.varzesh3.com/varzesh/news/1"),
    ("Sanctions hit the club's finances", "", "https://www.aljazeera.net/sport/2026/11/21/b"),
])
def test_sports_articles_are_not_relevant(triage, title, snippet, url):
    doc = Document(id="doc:x", source="t", url=url, title=title, snippet=snippet,
                   language="", published_at=NOW, fetched_at=NOW, kind="article")
    r = triage.assess(doc)
    assert r.relevance < 0.35 and "economic" not in r.categories, (title, r.scores)


def test_rial_still_counts_outside_sport(triage):
    r = triage.assess(_doc("سقوط ارزش ریال در برابر دلار به رکورد تازه رسید"))
    assert "economic" in r.categories
