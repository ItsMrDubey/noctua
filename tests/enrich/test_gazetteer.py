import pytest

from osint.enrich.gazetteer import Gazetteer, LazyGazetteer, best_place, fold, name_key
from tests.gazetteer_fixture import write_gazetteer


@pytest.fixture(scope="module")
def gz(tmp_path_factory):
    return Gazetteer.load(write_gazetteer(tmp_path_factory.mktemp("gaz")))


def _names(hits):
    return [(h.name, h.country, h.precision) for h in hits]


def test_fold_and_keys():
    assert fold("München") == "munchen"
    assert fold("أوكرانيا") == fold("اوكرانيا")  # hamza variants fold together
    assert name_key("Al-Hudaydah") == "al hudaydah"
    assert name_key("U.S.") == "u s"


def test_city_match_with_country_co_mention(gz):
    hits = gz.match("Russian missile strike hits Kharkiv, Ukraine says")
    assert ("Kharkiv", "UA", "city") in _names(hits)
    assert ("Ukraine", "UA", "country") in _names(hits)
    best = best_place(hits)
    assert best.name == "Kharkiv" and best.precision == "city"
    assert best.confidence >= 0.8
    assert best.geonameid == 706483


def test_multilingual_alternate_names(gz):
    assert best_place(gz.match("غارة إسرائيلية على بيروت")).name == "Beirut"
    assert best_place(gz.match("حمله پهپادی به اصفهان")).name == "Isfahan"
    assert best_place(gz.match("Масована атака дронів на Київ")).name == "Kyiv"
    assert best_place(gz.match("巴黎发生抗议")).name == "Paris"


def test_inflection_and_clitic_fallbacks(gz):
    hit = best_place(gz.match("Удар по Харькову: погибли три человека"))
    assert hit is not None and hit.name == "Kharkiv"
    hit = best_place(gz.match("قصف على الحديدة وبالحديدة"))
    assert hit is not None and hit.name == "Al Hudaydah"


def test_disambiguation_prefers_population_and_country(gz):
    paris = [h for h in gz.match("Protests in Paris") if h.name == "Paris"]
    assert paris[0].country == "FR"
    texas = [h for h in gz.match("Storm damage in Paris, United States")
             if h.name == "Paris"]
    assert texas[0].country == "US"  # co-mentioned country wins over population


def test_common_words_and_lowercase_are_not_places(gz):
    assert gz.match("police said the deal was nice") == []
    assert gz.match("Police said the weather was Nice") == []  # stopword single tokens
    assert gz.match("Tech stocks fall as IPO stalls") == []  # acronym vs alternate name


def test_maritime_area_and_country_alias(gz):
    hits = gz.match("Houthis claim attack on tanker in the Red Sea")
    assert ("Red Sea", "", "admin1") in _names(hits)
    assert ("Yemen", "YE", "country") in _names(hits)  # "Houthis" is a Yemen alias
    hits = gz.match("Israeli strikes reported near Hodeidah")
    assert ("Al Hudaydah", "YE", "city") in _names(hits)


def test_cache_roundtrip_and_rebuild(tmp_path):
    src = write_gazetteer(tmp_path / "g")
    first = Gazetteer.load(src)
    assert (src / "index.sqlite").exists()
    again = Gazetteer.load(src)
    assert len(again.places) == len(first.places) and again.names == first.names
    (src / "index.sqlite").write_bytes(b"not a database")
    assert Gazetteer.load(src).names == first.names  # corrupt cache is rebuilt


def test_lazy_gazetteer_missing_files(tmp_path, caplog):
    lazy = LazyGazetteer(tmp_path / "nothing")
    assert lazy.match("Kharkiv") == []
    assert lazy.available is False
    assert "gazetteer files missing" in caplog.text


def test_lazy_gazetteer_loads_once(tmp_path):
    lazy = LazyGazetteer(write_gazetteer(tmp_path / "g"))
    assert best_place(lazy.match("Kyiv")).name == "Kyiv"
    assert lazy.get() is lazy.get()

def test_surname_needs_a_same_country_mention(gz):
    # "Kim" is a town in India and a surname: no India mention, no Indian location
    hits = gz.match("Petition to oust Vitalii Kim as Ukraine's veterans minister grows")
    assert _names(hits) == [("Ukraine", "UA", "country")]
    hits = gz.match("Protest in Kim, India over water")
    assert ("Kim", "IN", "city") in _names(hits)


def test_title_case_headline_keeps_places(gz):
    hits = gz.match("Drone Strike Hits Pokrovsk, Russia Says")
    assert ("Pokrovsk", "UA", "city") in _names(hits)


def test_current_name_beats_another_citys_former_name(gz):
    # Engels (RU) was once "Pokrovsk": a Russia mention must not pull the town there
    hits = gz.match("Russian forces advance on Pokrovsk")
    assert ("Pokrovsk", "UA", "city") in _names(hits)
    assert ("Engels", "RU", "city") not in _names(hits)
