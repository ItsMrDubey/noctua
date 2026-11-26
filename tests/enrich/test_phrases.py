from osint.enrich.phrases import PhraseMatcher


def _m(*pairs):
    m = PhraseMatcher()
    for phrase, payload in pairs:
        m.add(phrase, payload)
    return m


def test_longest_phrase_wins_and_matches_do_not_overlap():
    m = _m(("Russian", "adj"), ("Russian forces", "forces"), ("forces", "noun"))
    assert [h.payload for h in m.find("Russian forces attack; forces regroup")] == [
        "forces", "noun"]


def test_whole_words_only():
    m = _m(("Iran", "IR"))
    assert m.find("Iranian officials said") == []
    assert [h.payload for h in m.find("Iran says")] == ["IR"]


def test_case_diacritics_and_punctuation_fold():
    m = _m(("cote d'ivoire army", "CI"), ("U.S. forces", "US"))
    hits = m.find("CÔTE D’IVOIRE ARMY and u.s. forces")
    assert [h.payload for h in hits] == ["CI", "US"]
    assert hits[1].text == "u.s forces" and (hits[1].start, hits[1].end) == (5, 8)


def test_trailing_star_is_a_prefix_on_the_last_word():
    m = _m(("беспилотник*", "drone"), ("cruise missile*", "cruise"))
    assert [h.payload for h in m.find("сбиты 20 беспилотников")] == ["drone"]
    assert [h.payload for h in m.find("two cruise missiles")] == ["cruise"]
    assert m.find("cruise ship") == []


def test_first_payload_of_a_phrase_is_kept():
    m = PhraseMatcher()
    assert m.add("IDF", 1) is True
    assert m.add("idf", 2) is False and m.add("  ", 3) is False
    assert len(m) == 1 and m.find("the IDF said")[0].payload == 1
