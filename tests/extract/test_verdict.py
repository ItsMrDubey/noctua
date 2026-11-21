import pytest

from osint.enrich.gazetteer import Gazetteer
from osint.pipeline.extract.verdict import gazetteer_geocoder, judge, normalise
from tests.gazetteer_fixture import write_gazetteer

TEXT = "Officials said two people were killed when a missile hit Kharkiv on Monday."
EVENT = {"id": "E1", "category": "armed_conflict", "country": "UA", "precision": "city",
         "lat": 49.98, "lon": 36.25, "place_name": "Kharkiv"}


def raw(**kw) -> dict:
    base = {"is_real_event": True, "event_type": "armed_conflict", "subtype": "missile strike",
            "summary": "A missile hit Kharkiv, killing two.", "when": "2026-11-20",
            "where": {"place_name": "Kharkiv", "admin1": None, "country": "ua",
                      "precision": "city"},
            "actors": [{"name": "Russia", "role": "perpetrator", "type": "state"},
                       {"name": "", "role": "x", "type": None}],
            "casualties": {"killed": 2, "injured": None}, "weapons_platforms": ["missile"],
            "cause_stated": None, "consequences": [], "claim_type": "official",
            "quotes": [{"field": "casualties", "quote": "two people were  killed"},
                       {"field": "where", "quote": "x" * 500},
                       {"field": "when", "quote": ""}]}
    base.update(kw)
    return base


def test_normalise_caps_and_checks_quotes():
    ext = normalise(raw(summary=" ".join(["word"] * 60), event_type="bogus",
                        claim_type="??"), TEXT)
    assert len(ext["summary"].split()) == 40
    assert ext["event_type"] == "other" and ext["claim_type"] == "unverified"
    assert ext["where"]["country"] == "UA"
    assert [a["name"] for a in ext["actors"]] == ["Russia"]
    q = ext["quotes"]
    assert len(q) == 2 and q[0]["verbatim"] is True
    assert len(q[1]["quote"]) == 200 and q[1]["verbatim"] is False


def test_normalise_survives_garbage():
    ext = normalise({"where": "nowhere", "casualties": [1], "quotes": "x", "actors": None,
                     "weapons_platforms": None}, "")
    assert ext["is_real_event"] is False and ext["where"]["precision"] == "unknown"
    assert ext["casualties"] == {"killed": None, "injured": None}


def test_unsupported_when_not_a_real_event():
    v = judge(EVENT, normalise(raw(is_real_event=False), TEXT))
    assert v.verdict == "unsupported"


def test_supported_same_place():
    v = judge(EVENT, normalise(raw(), TEXT),
              geocode=lambda p, a, c: (49.99, 36.23, "UA", "city"))
    assert v.verdict == "supported" and v.distance_km < 5 and v.geo["country"] == "UA"


def test_mismatch_country():
    ext = normalise(raw(where={"place_name": "Beirut", "admin1": None, "country": "LB",
                               "precision": "city"}), TEXT)
    v = judge(EVENT, ext)
    assert v.verdict == "mismatch" and "country" in v.reasons[0]


def test_mismatch_distance_same_country():
    ext = normalise(raw(where={"place_name": "Kyiv", "admin1": None, "country": "UA",
                               "precision": "city"}), TEXT)
    v = judge(EVENT, ext, geocode=lambda p, a, c: (50.45, 30.52, "UA", "city"))
    assert v.verdict == "mismatch" and v.distance_km > 300 and "km" in v.reasons[0]
    # coarse event location: distance not judged
    coarse = {**EVENT, "precision": "country"}
    assert judge(coarse, ext, geocode=lambda p, a, c: (50.45, 30.52, "UA", "city")).verdict \
        == "supported"


@pytest.mark.parametrize("cat,etype,expected", [
    ("armed_conflict", "economic", "mismatch"), ("armed_conflict", "other", "mismatch"),
    ("armed_conflict", "air_activity", "supported"), ("disaster", "armed_conflict", "mismatch"),
    ("political", "economic", "supported"), ("unknown_cat", "other", "supported")])
def test_type_compatibility(cat, etype, expected):
    assert judge({**EVENT, "category": cat}, normalise(raw(event_type=etype), TEXT)).verdict \
        == expected


def test_gazetteer_geocoder(tmp_path):
    gz = Gazetteer.load(write_gazetteer(tmp_path / "g"))
    geo = gazetteer_geocoder(gz)
    lat, _lon, cc, prec = geo("Kharkiv", "", "UA")
    assert cc == "UA" and prec == "city" and abs(lat - 49.98) < 0.01
    assert geo("Kharkiv", "", "FR") is None  # wrong country is not accepted
    ext = normalise(raw(where={"place_name": "Kyiv", "admin1": None, "country": "UA",
                               "precision": "city"}), TEXT)
    assert judge(EVENT, ext, geocode=geo).verdict == "mismatch"


def test_country_inferred_from_geocoded_place_when_not_stated():
    ext = normalise(raw(where={"place_name": "Gulf of Finland", "admin1": None, "country": None,
                               "precision": "admin1"}), TEXT)
    kal = {**EVENT, "country": "RU", "place_name": "Kaliningrad", "lat": 54.7, "lon": 20.5}
    v = judge(kal, ext, geocode=lambda p, a, c: (60.8, 24.9, "FI", "country"))
    assert v.verdict == "mismatch" and "geocodes to FI" in v.reasons[0]
    assert judge({**kal, "country": "FI"}, ext,
                 geocode=lambda p, a, c: (60.8, 24.9, "FI", "country")).verdict == "supported"
