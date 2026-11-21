from datetime import UTC, datetime

import pytest

from osint.naval import msi


@pytest.mark.parametrize("text,expected", [
    ("15-30.00N 055-45.00E", (15.5, 55.75)),
    ("15-30N 055-45E", (15.5, 55.75)),
    ("15-30.00 N 055-45.00 E", (15.5, 55.75)),
    ("15-30-36N 055-45-00E", (15.51, 55.75)),
    ("33-15.50S 151-30.00W", (-33.2583, -151.5)),
    ("1530N 05545E", (15.5, 55.75)),
    ("15.5N 055.75E", (15.5, 55.75)),
])
def test_parse_coords_formats(text, expected):
    (lat, lon), = msi.parse_coords(text)
    assert lat == pytest.approx(expected[0], abs=1e-3)
    assert lon == pytest.approx(expected[1], abs=1e-3)


def test_parse_coords_ignores_dates_and_numbers():
    assert msi.parse_coords("211500Z TO 212100Z NOV 26, ALTITUDE 1530 FEET") == []


WARNING = """NAVAREA IV 1234/26(GEN). NORTH ATLANTIC. VIRGINIA.
1. HAZARDOUS OPERATIONS, AIRCRAFT CARRIER FLIGHT OPERATIONS 211500Z TO 212100Z NOV
   IN AREAS BOUND BY:
   A. 37-50.00N 075-29.00W, 37-48.00N 075-20.00W,
      37-30.00N 074-00.00W, 37-25.00N 074-30.00W.
   B. 36-00.00N 073-00.00W, 36-10.00N 072-00.00W,
      35-40.00N 072-10.00W.
2. GUNNERY EXERCISES WITHIN 5 MILES OF 36-50.5N 076-17.25W.
3. TRACKLINE JOINING 30-00N 070-00W, 31-00N 069-00W.
4. CANCEL THIS MSG 212200Z NOV 26."""


def test_parse_geometries_polygons_circle_line():
    geoms = msi.parse_geometries(WARNING)
    types = [g["type"] for g in geoms]
    assert types == ["polygon", "polygon", "circle", "line"]
    a = geoms[0]["coords"]
    assert a[0] == a[-1] and len(a) == 5  # closed ring
    assert a[0] == [pytest.approx(-75.48333, abs=1e-4), pytest.approx(37.83333, abs=1e-4)]
    assert geoms[2]["radius_km"] == pytest.approx(5 * 1.852)
    assert geoms[3]["coords"] == [[-70.0, 30.0], [-69.0, 31.0]]


def test_single_point_and_summary():
    geoms = msi.parse_geometries("MISSILE FIRING IN VICINITY 12-00N 045-00E.")
    assert geoms == [{"type": "point", "coords": [[45.0, 12.0]]}]
    lat, lon, r = msi.geometry_summary(msi.parse_geometries(WARNING))
    assert 30 < lat < 38 and -77 < lon < -69 and r > 100


def test_dates_and_keywords():
    assert msi.parse_dtg("201530Z NOV 2026") == datetime(2026, 11, 20, 15, 30, tzinfo=UTC)
    assert msi.parse_dtg("010000Z JAN 27") == datetime(2027, 1, 1, tzinfo=UTC)
    assert msi.parse_dtg("garbage") is None
    assert msi.cancel_time(WARNING) == datetime(2026, 11, 21, 22, 0, tzinfo=UTC)
    s, m = msi.keyword_flags(WARNING, ["CARRIER", "FLIGHT OPERATIONS"],
                             ["GUNNERY", "HAZARDOUS OPERATIONS", "MISSILE FIRING"])
    assert s == ["CARRIER", "FLIGHT OPERATIONS"]
    assert m == ["GUNNERY", "HAZARDOUS OPERATIONS"]
