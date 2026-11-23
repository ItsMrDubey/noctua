from datetime import timedelta

from osint.storylines.config import LinkConfig, SensorConfig
from osint.storylines.features import case_info, name_phrases
from osint.storylines.link import Corpus, score_pair, score_sensor
from tests.storylines.fixtures import (
    BENGHAZI,
    benghazi_thread,
    finland_lookalike,
    kyiv_background,
    node,
)

CFG = LinkConfig()


def _corpus(*groups):
    return Corpus.of([n for g in groups for n in g])


def test_same_place_names_and_cosine_link_with_reasons():
    e1, e2, *_ = benghazi_thread()
    lk = score_pair(e1, e2, _corpus(benghazi_thread()), CFG)
    assert lk is not None and lk.linked
    kinds = {r["kind"]: r for r in lk.reasons}
    assert kinds["cosine"]["text"] == "cosine 0.87"
    # the best place relation counts: E1's documents name Benghazi, E2 is located there
    assert kinds["mentions"]["text"].endswith("names Benghazi")
    assert "Merkury" in kinds["terms"]["text"] and "shared names" in kinds["terms"]["text"]
    assert kinds["time"]["text"] == "+15 h" and kinds["time"]["points"] < 0
    assert lk.parts["place"] > 0 and lk.score >= CFG.link_threshold


def test_statement_naming_the_place_links_across_categories():
    e1, *_, e4 = benghazi_thread()
    lk = score_pair(e1, e4, _corpus(benghazi_thread()), CFG)
    assert lk.linked
    assert any(r["kind"] == "category" and r["text"] == "armed_conflict ~ political"
               for r in lk.reasons)


def test_topic_alone_never_links():
    # very similar text, same kind of incident, but no shared place / name: no anchor
    a = node("A", 0, "Drone attack on a ship in port", lat=10, lon=10, place="Alpha",
             country="AA")
    b = node("B", 2, "Drone strike hits a vessel in harbour", lat=40, lon=40, place="Beta",
             country="BB", angle=5)
    lk = score_pair(a, b, Corpus.of([a, b]), CFG)
    assert lk is not None and not lk.linked
    assert any(r["kind"] == "far" for r in lk.reasons)  # 4,000+ km: a penalty, not an anchor


def test_hard_gates_category_and_gap():
    a = node("A", 0, "Earthquake shakes the coast", category="disaster")
    b = node("B", 1, "Earthquake shakes the coast again", category="armed_conflict")
    assert score_pair(a, b, Corpus.of([a, b]), CFG) is None  # incompatible
    c = node("C", CFG.max_gap_h + 1, "Earthquake shakes the coast", category="disaster")
    assert score_pair(a, c, Corpus.of([a, c]), CFG) is None  # too far apart in time


def test_busy_place_weighs_a_quarter():
    bg = kyiv_background()
    corpus = Corpus.of(bg)
    assert corpus.busy({"p:kyiv"}, CFG)
    lk = score_pair(bg[3], bg[4], corpus, CFG)
    place = next(r for r in lk.reasons if r["kind"] == "same_place")
    assert "busy place" in place["text"] and place["points"] == CFG.same_place_points / 4
    assert not lk.linked  # "another strike on Kyiv" is background, not a thread
    # one incident's burst of coverage on one day does not make its place busy
    assert not _corpus(benghazi_thread()).busy({"p:benghazi"}, CFG)


def test_common_words_and_generic_words_weigh_less_than_rare_names():
    thread = benghazi_thread()
    filler = [node(f"X{i}", i, f"Russian attack {i} reported in the port area", angle=80)
              for i in range(60)]
    corpus = Corpus.of(thread + filler)
    assert corpus.is_name("w:merkury")
    assert corpus.term_weight("w:merkury", CFG) == 1.0
    assert corpus.term_weight("w:port", CFG) < 0.2  # in most of the window's titles


def test_case_info_and_name_phrases():
    caps, low = case_info("Russia says Kaliningrad-bound ship struck by drones in Gulf of "
                          "Finland")
    assert "w:kaliningrad" in caps and "w:drones" in low and "w:russia" not in caps  # 1st word
    assert case_info("Suspected Drone Attack Caused Explosion On Russian Ship") == (set(), set())
    assert name_phrases("Zelensky says Crimean Bridge strikes were a fair response") == {
        "n:crimean bridge"}
    assert name_phrases("Cargo ship attacked in Gulf of Finland waters") == {
        "n:gulf of finland"}


def test_sensor_event_links_by_place_and_time_only():
    e1 = benghazi_thread()[0]
    quake = node("S1", 3, "Seismic event", origin="sensor", category="armed_conflict",
                 lat=BENGHAZI[0] + 0.05, lon=BENGHAZI[1], detector="seismic", precision="point")
    lk = score_sensor(quake, e1, SensorConfig(), CFG)
    assert lk.linked and lk.reasons[0]["text"].startswith("seismic 5.6 km from Benghazi")
    far = node("S2", 3, "Seismic event", origin="sensor", lat=40.0, lon=20.0,
               detector="seismic", precision="point")
    assert score_sensor(far, e1, SensorConfig(), CFG) is None
    drought = node("S3", -2, "Drought", origin="sensor", lat=BENGHAZI[0], lon=BENGHAZI[1],
                   detector="hazard_drought", precision="point", span_h=24 * 400)
    assert score_sensor(drought, e1, SensorConfig(), CFG) is None
    late = node("S4", 100, "Seismic event", origin="sensor", lat=BENGHAZI[0], lon=BENGHAZI[1],
                detector="seismic", precision="point")
    assert score_sensor(late, e1, SensorConfig(), CFG) is None


def test_shared_sensor_detection_is_an_anchor():
    a, b = finland_lookalike()
    a.sensor_refs = b.sensor_refs = frozenset({"fire:viirs-1"})
    lk = score_pair(a, b, Corpus.of([a, b]), CFG)
    assert any(r["kind"] == "sensor" and "fire:viirs-1" in r["text"] for r in lk.reasons)
    assert lk.parts["sensor"] == CFG.sensor_ref_points
    assert lk.a == "F1" and (b.t - a.t) == timedelta(hours=4)
