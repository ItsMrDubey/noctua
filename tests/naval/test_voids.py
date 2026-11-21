import math
import random

import h3

from osint.naval import voids as V
from osint.naval.models import Evidence
from tests.naval.helpers import cfg, ts


def poisson(rng, lam):
    k, p, limit = 0, 1.0, math.exp(-lam)
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def setup_cells(n=40):
    c0 = h3.latlng_to_cell(0.0, 9.0, 4)
    return sorted(h3.grid_disk(c0, 4))[:n], c0


def baseline_and_recent(drop=(), lam=10.0, seed=7):
    rng = random.Random(seed)
    cells, _ = setup_cells()
    base_bins = list(range(100, 156))  # 14 days of 6 h bins
    counts = {c: {b: poisson(rng, lam) for b in base_bins} for c in cells}
    baseline = V.baseline_means(counts, base_bins)
    recent = {b: {c: (0 if c in drop else poisson(rng, lam)) for c in cells}
              for b in (156, 157, 158, 159)}
    return cells, baseline, recent


def test_poisson_cdf_matches_known_values_and_large_lambda():
    assert abs(V.poisson_cdf(0, 2.0) - math.exp(-2)) < 1e-12
    assert abs(V.poisson_cdf(2, 2.0) - 5 * math.exp(-2)) < 1e-12
    assert V.poisson_cdf(100, 1000.0) < 1e-100
    assert V.poisson_cdf(1000, 1000.0) > 0.5


def test_void_detected_against_baseline_and_merged():
    cells, _ = setup_cells()
    drop = ({cells[0], *h3.grid_ring(cells[0], 1)}) & set(cells)
    cells, baseline, recent = baseline_and_recent(drop=drop)
    found = V.detect_voids(baseline, recent, 160, cfg().sec("voids"), global_ratio=1.0)
    assert {f["cell"] for f in found} == drop
    assert all(f["p"] < 0.001 and f["observed"] == 0 for f in found)
    evs = V.void_evidence(found, ts(0), 1.0)
    assert len(evs) == 1 and evs[0].kind == "traffic_void"
    assert evs[0].polygon and evs[0].polygon[0] == evs[0].polygon[-1]
    assert len(evs[0].refs) == len(drop)


def test_no_false_voids_on_normal_traffic_and_feed_outage_guard():
    cells, baseline, recent = baseline_and_recent()
    assert V.detect_voids(baseline, recent, 160, cfg().sec("voids"), global_ratio=1.0) == []
    _, baseline, recent = baseline_and_recent(drop=set(cells))
    assert V.detect_voids(baseline, recent, 160, cfg().sec("voids"), global_ratio=0.1) == []


def test_sparse_baseline_cells_are_not_tested():
    _, baseline, recent = baseline_and_recent(drop=set(setup_cells()[0][:3]), lam=1.0)
    assert V.detect_voids(baseline, recent, 160, cfg().sec("voids"), global_ratio=1.0) == []


def _track(pts):
    return [(i, la, lo) for i, (la, lo) in enumerate(pts)]


def test_diversion_fraction_around_void():
    void = Evidence(kind="traffic_void", ts=ts(0), lat=0.0, lon=9.0, weight=1, description="",
                    radius_km=40, source_key="void:x")
    tracks = {
        "sea:a": _track([(-1.0, 7.0), (1.3, 9.0), (-1.0, 11.0)]),
        "sea:b": _track([(-0.9, 7.0), (1.4, 9.0), (-0.9, 11.0)]),
        "sea:c": _track([(1.0, 7.0), (-1.4, 9.0), (1.0, 11.0)]),
        "sea:d": _track([(-1.2, 7.0), (-1.2, 9.0), (-1.2, 11.0)]),   # straight
        "sea:far": _track([(10.0, 7.0), (12.0, 9.0), (10.0, 11.0)]),  # not near the void
    }
    evs = V.diversion_evidence([void], tracks, cfg().sec("voids"), ts(0))
    assert len(evs) == 1 and "3/4" in evs[0].description
    tracks.pop("sea:a")
    tracks.pop("sea:b")
    assert V.diversion_evidence([void], tracks, cfg().sec("voids"), ts(0)) == []


def test_cells_res4_rollup():
    c5 = h3.latlng_to_cell(0.0, 9.0, 5)
    sib = sorted(h3.cell_to_children(h3.cell_to_parent(c5, 4), 5))[:2]
    rows = [(h3.str_to_int(sib[0]), 7, 3), (h3.str_to_int(sib[1]), 7, 4), (None, 7, 9)]
    out = V.cells_res4(rows)
    assert dict(out[7]) == {h3.cell_to_parent(c5, 4): 7}
