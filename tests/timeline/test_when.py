from datetime import UTC, datetime

import pytest

from osint.timeline.when import local_offset_h, parse_when

PUB = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)  # a Saturday
LON_KYIV = 30.5  # local offset +2 by longitude


def band(phrase, pub=PUB, lon=LON_KYIV):
    b = parse_when(phrase, pub, lon)
    return None if b is None else b.as_dict()


def test_local_offset_from_longitude():
    assert local_offset_h(30.5) == 2.0 and local_offset_h(-74) == -5.0 and local_offset_h(None) == 0


def test_date_only_is_the_whole_local_day_never_a_point():
    b = band("Nov 20 (Reuters)")
    assert (b["lo"], b["hi"], b["precision"]) == ("2026-11-19T22:00+00:00", "2026-11-20T22:00+00:00",
                                                  "date")
    assert b["tz_basis"] == "longitude" and not b["clamped"]


@pytest.mark.parametrize("phrase,lo,hi,qual", [
    ("в ніч на п'ятницю, 20 листопада", "2026-11-19T18:00", "2026-11-20T04:00", "overnight"),
    ("early on Nov. 20", "2026-11-19T22:00", "2026-11-20T07:00", "early"),
    ("late on Nov. 20", "2026-11-20T16:00", "2026-11-20T22:00", "late"),
    ("shortly after midnight on November 20", "2026-11-19T22:00", "2026-11-20T01:00",
     "shortly after midnight"),
])
def test_part_of_day(phrase, lo, hi, qual):
    b = band(phrase)
    assert b["precision"] == "part_of_day" and b["qualifier"] == qual
    assert (b["lo"], b["hi"]) == (lo + "+00:00", hi + "+00:00")


def test_stated_zone_is_minute_precision_and_unstated_is_widened():
    b = band("close at 8:00 a.m. Moscow time on November 20")
    assert (b["lo"], b["precision"], b["tz_basis"]) == ("2026-11-20T05:00+00:00", "minute", "stated")
    b = band("землетрясение произошло 20 ноября в 19:09 по местному времени")
    assert b["precision"] == "time_local"
    assert (b["lo"], b["hi"]) == ("2026-11-20T16:09+00:00", "2026-11-20T18:09+00:00")


def test_weekday_and_relative_days_resolve_before_publication():
    assert band("On Friday")["lo"] == "2026-11-19T22:00+00:00"
    assert band("samedi")["lo"] == "2026-11-20T22:00+00:00"  # the publication day itself
    assert band("yesterday")["lo"] == "2026-11-19T22:00+00:00"
    assert band("صباح اليوم السبت")["qualifier"] == "morning"


def test_clamped_to_publication_and_future_flagged():
    b = band("21 novembre")  # the publication day: cannot have happened after 09:00 UTC
    assert b["clamped"] and b["hi"] == "2026-11-21T09:00+00:00"
    b = band("all land crossings will close on December 1")
    assert b["future"] and b["lo"].startswith("2026-11-30T22:00")  # next week, not last year


def test_range_and_bounds():
    b = band("Between the morning of November 19 and the morning of November 20")
    assert b["precision"] == "range" and b["lo"] == "2026-11-18T22:00+00:00"
    assert band("through November 21, 2026") is None  # a bound, not an occurrence
    assert band("since Nov 19") is None


def test_unparseable_and_iso_numeric():
    assert band("in the third quarter") is None and band("") is None and band(None) is None
    assert band("At 0406, the vessel capsized") is None  # a time with no day anchor
    assert band("Issued on: 20/11/2026")["lo"] == "2026-11-19T22:00+00:00"
    assert band("2026-11-20")["precision"] == "date"
    assert band("In the early hours of August 19, 2024")["lo"].startswith("2024-08-18")
