import pytest

from osint.fires.geo import PointIndex, PolygonIndex, SegmentIndex, dist_km, geojson_polygons
from osint.fires.score import CountGrid


def test_dist_km_matches_degrees():
    assert dist_km(0, 0, 1, 0) == pytest.approx(111.2, abs=0.1)
    assert dist_km(60, 10, 60, 11) == pytest.approx(55.6, abs=0.2)
    assert dist_km(0, 179.95, 0, -179.95) == pytest.approx(11.1, abs=0.1)  # antimeridian


def test_point_index_within_count_nearest():
    idx = PointIndex([(50.0, 30.0, "a"), (50.01, 30.0, "b"), (50.5, 30.0, "c")], cell_deg=0.05)
    hits = idx.within(50.0, 30.0, 2.0)
    assert [p for _, p in hits] == ["a", "b"]
    assert idx.count(50.0, 30.0, 2.0) == 2
    assert idx.count(50.0, 30.0, 60.0) == 3
    assert idx.nearest(50.4, 30.0, 20.0)[1] == "c"
    assert idx.nearest(10.0, 10.0, 20.0) is None
    assert len(idx) == 3


def test_point_index_wraps_antimeridian():
    idx = PointIndex([(0.0, 179.99, "east")], cell_deg=0.25)
    assert idx.count(0.0, -179.99, 5.0) == 1


def test_segment_index_distance():
    seg = SegmentIndex([[(48.0, 37.0), (48.0, 38.0)]], cell_deg=0.1)
    assert seg.distance(48.1, 37.5, 50) == pytest.approx(11.1, abs=0.1)
    assert seg.distance(48.0, 38.2, 50) == pytest.approx(14.9, abs=0.2)  # past the end
    assert seg.distance(49.0, 37.5, 50) is None


def test_polygon_index_with_hole_and_multipolygon():
    outer = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
    hole = [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]
    polys = geojson_polygons({"type": "MultiPolygon", "coordinates": [
        [outer, hole], [[[20, 0], [21, 0], [21, 1], [20, 1], [20, 0]]]]})
    idx = PolygonIndex(polys)
    assert idx.contains(1, 1) and idx.contains(0.5, 20.5)
    assert not idx.contains(5, 5)  # in the hole
    assert not idx.contains(11, 5) and not idx.contains(5, 15)
    assert geojson_polygons({"type": "Point", "coordinates": [0, 0]}) == []


def test_count_grid_approx():
    g = CountGrid(0.1)
    for i in range(10):
        g.add(50.0 + i * 0.01, 30.0)
    g.add(52.0, 30.0)
    assert g.approx(50.05, 30.0, 25) == 10
    assert g.approx(50.05, 30.0, 300) == 11
