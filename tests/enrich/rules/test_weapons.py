import pytest

from osint.enrich.rules import RuleSet
from osint.enrich.rules.base import ExtractDoc
from osint.enrich.rules.weapons import weapons


@pytest.fixture(scope="module")
def lexicon():
    return RuleSet.load().weapons


@pytest.mark.parametrize(("title", "labels"), [
    ("Russian drones and a Kh-101 cruise missile hit Kyiv", ["cruise missile", "drone"]),
    ("Missile strike on Dnipro", ["missile"]),
    ("Ballistic missile and more missiles", ["ballistic missile"]),
    ("Think tank warns of tank shortage", ["tank"]),
    ("Car bomb kills 5", ["VBIED"]),
    ("Удар баллистической ракетой по Одессе", ["ballistic missile"]),
    ("غارة جوية على غزة", ["airstrike"]),
    ("Shahed drones attack Odesa", ["drone"]),
    ("Sea drone hits Russian warship", ["naval drone", "warship"]),
    ("Shell company profits rise", [])])
def test_weapon_labels(lexicon, title, labels):
    vals = weapons({}, [ExtractDoc(doc_id="d1", title=title)], lexicon=lexicon)
    assert [v.value for v in vals] == labels
    assert all(v.field == "weapons_platforms" and v.doc_id == "d1" for v in vals)


def test_first_mention_is_quoted(lexicon):
    docs = [ExtractDoc(doc_id="late", title="Drones again", published_at="2026-11-25T12"),
            ExtractDoc(doc_id="early", title="x", snippet="FPV drones struck a tank",
                       published_at="2026-11-25T09")]
    vals = {v.value: v for v in weapons({}, docs, lexicon=lexicon)}
    assert vals["drone"].doc_id == "early" and vals["drone"].quote == "FPV drones"
