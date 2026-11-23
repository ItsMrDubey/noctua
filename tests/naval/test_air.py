import math
from datetime import timedelta

from osint.naval import indicators as I
from osint.naval import legs as L
from tests.naval.helpers import NOW, cfg, synthetic_geo, ts

US = 1_000_000


def row(tid, minutes, lat, lon, alt, spd=150.0, hdg=90.0):
    return (tid, int(ts(minutes).timestamp() * US), lat, lon, alt, spd, hdg)


def run(rows, metas, window_start=None, now=None):
    return I.air_indicators(rows, metas, synthetic_geo(), cfg(),
                            window_start=window_start or ts(-600), now=now or ts(120))


def test_leg_segmentation():
    pts = [L.Pt(ts(m), 0, 10, 1000, 100, 0) for m in (0, 5, 10, 60, 65)]
    legs = L.segment_legs("air:x", pts, window_start=ts(-60), now=ts(66),
                          max_gap=timedelta(minutes=20), live_grace=timedelta(minutes=20))
    assert [len(x.points) for x in legs] == [3, 2]
    assert legs[0].starts_in_window and legs[0].ended
    assert legs[1].starts_in_window and not legs[1].ended   # still live


def test_c2_disappearing_low_over_open_ocean_is_sea_terminating():
    # C-2 descends and vanishes ~900 km east of the island (open ocean)
    rows = [row("air:c2", m, 0.0, 6.0 + m * 0.02, alt, 140) for m, alt in
            ((0, 9000), (10, 5000), (20, 2500), (25, 1200))]
    evs = run(rows, {"air:c2": {"type_code": "C2", "name": "RAWHIDE1"}})
    st = [e for e in evs if e.kind == "sea_terminating_flight"]
    assert len(st) == 1   # the leg starts at 9000 ft: only the low disappearance counts
    end = st[0]
    assert "disappeared" in end.description and end.weight == 3.0
    assert end.refs[0]["id"] == "air:c2"


def test_airport_terminating_leg_is_not_counted():
    # same profile but ending at the island's airport (0, 0) and starting over land
    rows = [row("air:c2b", m, 0.0, 2.0 - m * 0.08, alt, 140) for m, alt in
            ((0, 9000), (10, 5000), (20, 2500), (25, 1000))]
    evs = run(rows, {"air:c2b": {"type_code": "C2"}})
    assert not [e for e in evs if e.kind == "sea_terminating_flight"]


def test_high_altitude_coverage_loss_is_not_counted():
    rows = [row("air:p", m, 0.0, 8.0 + m * 0.05, 30000, 450) for m in (0, 10, 20)]
    evs = run(rows, {"air:p": {"type_code": "E2"}})
    assert not [e for e in evs if e.kind == "sea_terminating_flight"]
    assert [e for e in evs if e.kind == "cvw_aircraft"]          # but E-2 far out is a CVW sign


def test_excluded_patrol_type_and_generic_military_weight():
    rows = [row("air:p8", m, 0.0, 8.0, 800, 180) for m in (0, 5, 10)]
    assert not run(rows, {"air:p8": {"type_code": "P8"}})
    rows = [row("air:h", m, 0.0, 8.0, 800, 120) for m in (0, 5, 10)]
    # unlisted military types are ignored by default (generic_weight_factor: 0) ...
    assert not [e for e in run(rows, {"air:h": {"type_code": "XYZ"}})
                if e.kind == "sea_terminating_flight"]
    # ... and get the configured share when enabled
    c = cfg()
    c["flights"]["generic_weight_factor"] = 0.5
    evs = [e for e in I.air_indicators(rows, {"air:h": {"type_code": "XYZ"}}, synthetic_geo(), c,
                                       window_start=ts(-600), now=ts(120))
           if e.kind == "sea_terminating_flight"]
    assert evs and all(e.weight == 0.5 for e in evs)


def test_still_live_leg_end_is_not_terminating():
    rows = [row("air:v", m, 0.0, 8.0, 900, 120) for m in (0, 5, 10)]
    evs = run(rows, {"air:v": {"type_code": "V22"}}, window_start=ts(-600), now=ts(15))
    st = [e for e in evs if e.kind == "sea_terminating_flight"]
    assert len(st) == 1 and "appeared" in st[0].description


def test_orbit_detection_far_from_land():
    rows = []
    for i in range(48):  # 2 full circles of radius ~40 km over 96 minutes
        a = i / 24 * 2 * math.pi
        rows.append(row("air:k", i * 2, 0.36 * math.sin(a), 9.0 + 0.36 * math.cos(a), 25000, 300))
    evs = run(rows, {"air:k": {"type_code": "K35R"}}, now=ts(300))
    orb = [e for e in evs if e.kind == "support_orbit"]  # land-based tanker: supporting
    assert len(orb) == 1 and orb[0].weight == 1.5
    assert abs(orb[0].lon - 9.0) < 0.2
    # a straight transit is not an orbit
    rows = [row("air:t", i * 2, 0.0, 6.0 + i * 0.1, 25000, 300) for i in range(48)]
    assert not [e for e in run(rows, {"air:t": {"type_code": "K35R"}}) if "orbit" in e.kind]
    assert NOW
