"""Synthetic leg fixtures for carrier-aircraft fixes and the moving-point fit."""

import math
import random
from datetime import timedelta

from osint.core.geo import haversine_km
from osint.naval import airfix as AF
from osint.naval import fusion as F
from osint.naval import indicators as I
from osint.naval import trackfit as T
from osint.naval.geo import AirportIndex
from tests.naval.helpers import NOW, cfg, synthetic_geo, ts

US = 1_000_000
KT = 1.852


def row(tid, minutes, lat, lon, alt, spd=140.0):
    return (tid, int(ts(minutes).timestamp() * US), lat, lon, alt, spd, 0.0)


def fixes(rows, metas, geo=None, now=None, c=None):
    return I.air_fixes(rows, metas, geo or synthetic_geo(), c or cfg(),
                       window_start=ts(-24 * 60), now=now or ts(24 * 60))


def ship(t_h, lat0=0.0, lon0=8.0, kts=20.0, hdg=45.0):
    """Synthetic carrier: straight line at kts / hdg from (lat0, lon0) at t=0."""
    d = kts * KT * t_h
    return (lat0 + d * math.cos(math.radians(hdg)) / 110.57,
            lon0 + d * math.sin(math.radians(hdg)) / (111.32 * math.cos(math.radians(lat0))))


def fix(tid, t_h, lat, lon, w=2.0, role="recovery", typ="V22"):
    return AF.AirFix(track_id=tid, ts=NOW + timedelta(hours=t_h), lat=lat, lon=lon, role=role,
                     type_code=typ, callsign="", label=typ, weight=w, alt_ft=500, speed_kts=120,
                     airfield_km=math.inf, coast_km=500, reason="")


# --------------------------------------------------------------------- fixes


def test_launch_and_recovery_signatures_from_legs():
    # CMV-22 appears at 400 ft and climbs out east of the island, later descends and vanishes
    up = [row("air:v1", m, 0.0, 8.0 + m * 0.03, alt, 160) for m, alt in
          ((0, 400), (2, 1500), (4, 3500), (6, 6000), (10, 9000))]
    down = [row("air:v1", 300 + m, 0.5, 9.0 + m * 0.02, alt, 140) for m, alt in
            ((0, 9000), (5, 6000), (10, 3000), (14, 900))]
    fx = fixes(up + down, {"air:v1": {"type_code": "V22", "name": "RAWHIDE34", "is_mil": True}})
    roles = sorted(f.role for f in fx)
    assert roles == ["launch", "recovery"]
    rec = next(f for f in fx if f.role == "recovery")
    assert rec.weight == 3.0  # callsign weight (3.0) beats the V22 multiplier (2.5)
    d = rec.describe()
    assert "RAWHIDE34" in d and "last fix 900 ft" in d and "140 kt" in d
    assert "no airfield within 600 km" in d and "carrier onboard delivery" in d


def test_descending_through_coverage_edge_high_is_not_a_fix():
    rows = [row("air:e2", m, 0.0, 8.0 + m * 0.05, 25000, 300) for m in (0, 5, 10)]
    assert not fixes(rows, {"air:e2": {"type_code": "E2"}})


def test_offshore_platform_guard_for_helicopters():
    geo = synthetic_geo()
    rows = [row("air:h", m, 0.0, 8.0 + m * 0.002, 300, 90) for m in (0, 5, 10)]
    meta = {"air:h": {"type_code": "H60"}}
    assert fixes(rows, meta, geo=geo)
    geo.platforms = AirportIndex([(0.0, 8.05, "flare")])
    assert not fixes(rows, meta, geo=geo)


def test_patrol_types_and_generic_military_never_fix():
    rows = [row("air:p", m, 0.0, 8.0, 500, 180) for m in (0, 5, 10)]
    assert not fixes(rows, {"air:p": {"type_code": "P8", "is_mil": True}})
    assert not fixes(rows, {"air:p": {"type_code": "A139", "is_mil": True}})  # AW139 (oil & gas)
    assert not fixes(rows, {"air:p": {"type_code": "E170", "is_mil": True}})


def test_jasdf_kawasaki_c2_hex_block_is_excluded():
    rows = [row(tid, m, 0.0, 8.0 + m * 0.02, alt, 140)
            for tid in ("air:8501ab", "air:ae1234")
            for m, alt in ((0, 9000), (10, 4000), (20, 800))]
    fx = fixes(rows, {"air:8501ab": {"type_code": "C2"}, "air:ae1234": {"type_code": "C2"}})
    assert [f.track_id for f in fx] == ["air:ae1234"]


def test_fighter_training_sortie_from_coastal_airfield_is_downweighted():
    geo = synthetic_geo(extra_airports=[(0.0, 2.2, "XCST")])
    rows = [row("air:f", m, 0.0, 2.2 + m * 0.07, alt, 300 if alt > 3000 else 180)
            for m, alt in ((0, 0), (5, 6000), (10, 12000), (15, 8000), (20, 1200))]
    fx = fixes(rows, {"air:f": {"type_code": "F18S"}}, geo=geo)
    assert len(fx) == 1 and fx[0].role == "recovery"
    assert abs(fx[0].weight - 2.5 * 0.35) < 1e-9 and "training-range" in fx[0].describe()


def test_helicopter_loiter_around_a_moving_point():
    rows = []
    for i in range(31):  # 60 min of circles (r ~10 km) around a centre drifting 15 kt north
        a = i / 6 * 2 * math.pi
        clat = 15 * KT * (i * 2 / 60) / 110.57
        rows.append(row("air:sh", 30 + i * 2, clat + 0.09 * math.sin(a), 9.0 + 0.09 * math.cos(a),
                        500, 90))
    # appeared + vanished at altitude: only the loiter can make a fix
    rows = [row("air:sh", 0, 0.0, 9.0, 4000, 120), *rows, row("air:sh", 100, 0.2, 9.0, 4000, 120)]
    fx = fixes(rows, {"air:sh": {"type_code": "H60"}})
    lo = [f for f in fx if f.role == "loiter"]
    assert len(lo) == 1 and abs(lo[0].lon - 9.0) < 0.1 and "loitered" in lo[0].describe()
    # the same circles drifting at 120 kt are an aircraft in transit, not a ship's helicopter
    fast = [(r[0], r[1], r[2] + (r[1] - rows[0][1]) / US / 3600 * 120 * KT / 110.57, *r[3:])
            for r in rows]
    assert not [f for f in fixes(fast, {"air:sh": {"type_code": "H60"}}) if f.role == "loiter"]


def test_position_glitch_is_dropped():
    rows = [row("air:v", m, 0.0, 8.0 + m * 0.02, alt, 140) for m, alt in
            ((0, 9000), (10, 5000), (20, 1000))]
    rows.append(row("air:v", 21, 25.0, 8.4, 0, 0))  # 2500 km jump in a minute
    fx = fixes(rows, {"air:v": {"type_code": "V22"}})
    assert len(fx) == 1 and abs(fx[0].lat) < 0.1


# ----------------------------------------------------------------------- fit


def _carrier_fixes(rng, n=10, noise_km=10.0, **kw):
    out = []
    for k in range(n):
        t = 1.0 + k * 1.2
        la, lo = ship(t, **kw)
        la += rng.gauss(0, noise_km) / 110.57
        lo += rng.gauss(0, noise_km) / 111.32
        out.append(fix(f"air:{k % 4}", t, la, lo, typ=("V22", "H60", "E2", "C2")[k % 4]))
    return out


def test_fit_recovers_position_speed_and_heading():
    rng = random.Random(7)
    fx = _carrier_fixes(rng)
    fits = T.fit_tracks(fx, cfg().sec("trackfit"))
    assert len(fits) == 1
    f = fits[0]
    assert len(f.fixes) == 10 and f.velocity_known
    assert abs(f.speed_kts - 20) < 6 and abs(F_angle(f.heading_deg, 45)) < 20
    now = NOW + timedelta(hours=14)
    d = f.to_dict(now, cfg().sec("trackfit"))
    truth = ship(14)
    err = haversine_km(d["projected"]["lat"], d["projected"]["lon"], *truth)
    assert err < 30
    assert err < d["ellipse"]["semi_major_km"]  # the 95 % ellipse covers the truth


def F_angle(a, b):
    return (a - b + 180) % 360 - 180


def test_ellipse_grows_with_time_since_last_fix():
    f = T.fit_tracks(_carrier_fixes(random.Random(3)), cfg().sec("trackfit"))[0]
    tc = cfg().sec("trackfit")
    e1 = f.ellipse(f.t_last + timedelta(hours=1), tc)
    e2 = f.ellipse(f.t_last + timedelta(hours=24), tc)
    assert e2["semi_major_km"] > e1["semi_major_km"] * 2
    assert e1["semi_major_km"] >= e1["semi_minor_km"] > 0


def test_speed_is_clamped_to_carrier_speed():
    fx = [fix("air:a", 0, 0.0, 8.0), fix("air:b", 2, 0.0, 8.0 + 2 * 45 * KT / 111.32)]
    fits = T.fit_tracks(fx, {**cfg().sec("trackfit"), "inlier_km": 200})
    assert all(f.speed_kts <= 32.01 for f in fits)


def test_separate_operating_areas_and_outliers_form_separate_fits():
    rng = random.Random(11)
    med = _carrier_fixes(rng, n=6, lat0=35.0, lon0=18.0, kts=12, hdg=90)
    red = _carrier_fixes(rng, n=6, lat0=20.0, lon0=38.5, kts=10, hdg=330)
    # same time, 300 km from the Med group: cannot be the same ship
    odd = [fix("air:x", 3.0, 35.0, 21.5)]
    fits = T.fit_tracks(med + red + odd, cfg().sec("trackfit"))
    sizes = sorted(len(f.fixes) for f in fits)
    assert sizes == [1, 6, 6]
    big = [f for f in fits if len(f.fixes) == 6]
    lats = sorted(f.at(f.t_last)[0] for f in big)
    assert abs(lats[0] - 20.5) < 1 and abs(lats[1] - 35) < 1


def test_fit_evidence_counts_aircraft_not_fixes():
    tc = cfg().sec("trackfit")
    one = [fix("air:a", t, 0.0, 8.0 + t * 0.05) for t in (0, 1, 2, 3)]
    many = [fix(f"air:{t}", t, 0.0, 8.0 + t * 0.05) for t in (0, 1, 2, 3)]
    e1 = T.fit_evidence(T.fit_tracks(one, tc), NOW + timedelta(hours=4), tc)[0]
    e4 = T.fit_evidence(T.fit_tracks(many, tc), NOW + timedelta(hours=4), tc)[0]
    assert e1.kind == e4.kind == "air_track"
    assert e4.weight > e1.weight and e4.source_keys == ["air:0", "air:1", "air:2", "air:3"]
    assert e1.detail["ellipse"]["confidence"] == 0.95


# -------------------------------------------------------------------- fusion


def _fuse(evs):
    c = cfg()
    s = F.build_surface(evs, NOW + timedelta(hours=4), c.sec("fusion"), synthetic_geo().mask)
    return F.hypotheses(evs, s, c.sec("fusion"))


def test_fit_and_its_own_flights_are_one_independent_signal():
    tc = cfg().sec("trackfit")
    fx = [fix("air:c2", 3.5, 0.0, 9.0, w=3.0)]
    fit_ev = T.fit_evidence(T.fit_tracks(fx, tc), NOW + timedelta(hours=4), tc)
    from osint.naval.models import Evidence
    st = Evidence(kind="sea_terminating_flight", ts=NOW + timedelta(hours=3.5), lat=0.0,
                  lon=9.0, weight=3.0, description="", source_key="air:c2")
    h = _fuse([st, *fit_ev])[0]
    assert set(h["kinds"]) == {"sea_terminating_flight", "air_track"}
    assert len(h["independent_kinds"]) == 1 and h["confidence"] <= 0.45
    assert h["air_track"]["aircraft"] == ["air:c2"]


def test_several_aircraft_on_one_moving_point_lift_the_cap_but_not_beyond():
    tc = cfg().sec("trackfit")
    rng = random.Random(5)
    fx = _carrier_fixes(rng, n=8, noise_km=5)
    evs = T.fit_evidence(T.fit_tracks(fx, tc), NOW + timedelta(hours=10), tc)
    c = cfg()
    s = F.build_surface(evs, NOW + timedelta(hours=10), c.sec("fusion"), synthetic_geo().mask)
    h = F.hypotheses(evs, s, c.sec("fusion"))[0]
    assert 0.45 < h["confidence"] <= c.sec("fusion")["air_multi_cap"]
    assert h["centre_basis"] == "air-track fit projected to now"
    truth = ship(10)
    assert haversine_km(h["lat"], h["lon"], *truth) < h["radius_km"]


def test_aircraft_kinds_are_one_family_for_the_two_kind_rule():
    fc = cfg().sec("fusion")
    st = {"sea_terminating_flight": 0.8, "cvw_aircraft": 0.6}
    c, _, indep = F.confidence(st, fc, {"sea_terminating_flight": {"air:c2"},
                                        "cvw_aircraft": {"air:e2"}})
    assert len(indep) == 2 and c <= fc["single_kind_cap"]   # two flights, one modality
    c2, _, _ = F.confidence({**st, "support_ship": 0.8}, fc,
                            {"sea_terminating_flight": {"air:c2"}, "cvw_aircraft": {"air:e2"},
                             "support_ship": {"sea:1"}})
    assert c2 > 0.6
    c3, _, _ = F.confidence({**st, "air_track": 0.9}, fc, None, air_multi=True)
    assert fc["single_kind_cap"] < c3 <= fc["air_multi_cap"]


def test_earlier_position_of_a_fitted_ship_is_not_a_second_hypothesis():
    from osint.naval.models import Evidence
    tc = cfg().sec("trackfit")
    now = NOW + timedelta(hours=20)
    fx = [fix("air:a", 0.0, 0.0, 4.0, w=3.0), fix("air:b", 18.0, 0.0, 8.0, w=3.0)]
    evs = T.fit_evidence(T.fit_tracks(fx, tc), now, tc)
    evs += [Evidence(kind="sea_terminating_flight", ts=f.ts, lat=f.lat, lon=f.lon, weight=3.0,
                     description="", source_key=f.track_id) for f in fx]
    c = cfg()
    s = F.build_surface(evs, now, c.sec("fusion"), synthetic_geo().mask)
    hyps = F.hypotheses(evs, s, c.sec("fusion"))
    assert len(hyps) == 1 and hyps[0]["lon"] > 7.5
    assert hyps[0]["air_track"]["velocity_known"] and len(hyps[0]["air_track"]["path"]) == 3


def test_tanker_or_patrol_orbits_alone_never_make_a_hypothesis():
    rows = []
    for i in range(48):
        a = i / 24 * 2 * math.pi
        for tid in ("air:k", "air:e"):
            lon0 = 9.0 if tid == "air:k" else -9.0
            rows.append(row(tid, i * 2, 0.36 * math.sin(a), lon0 + 0.36 * math.cos(a), 25000, 300))
    metas = {"air:k": {"type_code": "P8"}, "air:e": {"type_code": "E2"}}
    evs = I.air_indicators(rows, metas, synthetic_geo(), cfg(), window_start=ts(-600),
                           now=ts(300))
    assert {e.kind for e in evs if "orbit" in e.kind} == {"support_orbit", "orbit"}
    c = cfg()
    s = F.build_surface(evs, ts(300), c.sec("fusion"), synthetic_geo().mask)
    hyps = F.hypotheses(evs, s, c.sec("fusion"))
    assert [round(h["lon"]) for h in hyps] == [-9]   # only the carrier-type (E-2) orbit
