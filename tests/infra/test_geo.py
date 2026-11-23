from osint.infra.geo import PointGrid, SegmentGrid, seg_dist_km, simplify


def test_simplify_keeps_ends_and_corners():
    line = [[0, 0], [1, 0.0001], [2, 0], [2, 1], [2, 2]]
    out = simplify(line, 0.01)
    assert out[0] == [0, 0] and out[-1] == [2, 2]
    assert [2, 0] in out and [1, 0.0001] not in out
    assert simplify([[0, 0], [1, 1]], 0.1) == [[0, 0], [1, 1]]


def test_seg_dist_km():
    # 0.01 deg north of a segment along the equator ~ 1.11 km
    d = seg_dist_km(0.01, 0.5, (0.0, 0.0), (1.0, 0.0))
    assert 1.05 < d < 1.17
    # beyond the end: distance to the endpoint
    assert seg_dist_km(0.0, 1.1, (0.0, 0.0), (1.0, 0.0)) > 10


def test_segment_grid_near_and_antimeridian():
    g = SegmentGrid(cell_deg=0.25, buffer_km=2.0)
    g.add_line([[10.0, 55.0], [11.0, 55.0]], {"id": "c1"})
    g.add_line([[179.9, 0.0], [-179.9, 0.0]], {"id": "hop"})  # skipped
    hit = g.near(55.01, 10.5)
    assert hit and hit[0]["id"] == "c1" and hit[1] < 1.2
    assert g.near(55.05, 10.5) is None  # ~5.5 km away
    assert g.near(55.01, 10.5, km=0.5) is None
    assert g.near(0.0, 179.95) is None and len(g) == 1


def test_point_grid():
    g = PointGrid(0.5)
    g.add(59.0, 24.0, {"n": "a"})
    assert g.near(59.03, 24.0, 5)[0]["n"] == "a"
    assert g.near(59.2, 24.0, 5) is None
