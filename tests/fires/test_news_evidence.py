import pytest

from osint.fires.job import conflict_evidence


@pytest.mark.parametrize("title,verdict,counts", [
    # a denial is not conflict evidence (scored a refinery-area hotspot in Ahvaz war-likely)
    (("NOTE: Contrary to reports, there are no missile launches identified from Khuzestan / "
      "Ahvaz at this time."), None, False),
    ("No explosions were reported in Ahvaz, officials say", None, False),
    ("Air raid alert in Kharkiv was a false alarm", None, False),
    # extraction found the article reports no concrete event
    ("Missile strike on Kyiv", "unsupported", False),
    # real attacks, including ones a party denies or without casualties, still count
    ("Drone strike hits Ahvaz refinery", None, True),
    ("Iran denies drone attack on Ahvaz", None, True),
    ("No casualties reported after drone attack on Odesa port", None, True),
    ("Russian missile strike on Kyiv kills 2", "supported", True),
    ("Russian missile strike on Kyiv kills 2", "mismatch", True),
])
def test_conflict_evidence(title, verdict, counts):
    assert conflict_evidence({"title": title}, verdict) is counts
