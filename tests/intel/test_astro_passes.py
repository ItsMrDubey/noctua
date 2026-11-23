"""Sun / moon (osint.intel.astro) against published almanac values, and overhead pass
prediction (osint.intel.passes) against orbital invariants and a brute-force search."""
import math
from datetime import UTC, datetime

import numpy as np
import pytest

from osint.intel import astro, passes
from tests.intel.conftest import SAT_ROWS

pytest.importorskip("sgp4")


def utc(*a):
    return datetime(*a, tzinfo=UTC)


# ------------------------------------------------------------------ sun / moon

def test_sun_elevation_and_azimuth():
    # Greenwich, June solstice noon: 90 - 51.48 + 23.44 = 61.96 deg, due south
    el, az = astro.sun_position(utc(2024, 6, 21, 12, 0), 51.4779, 0.0)
    assert el == pytest.approx(61.96, abs=0.1) and az == pytest.approx(180, abs=2)
    # equinox, sub-solar point near (0, 0) around 12:07 UTC (equation of time ~ -7 min)
    assert astro.sun_position(utc(2024, 3, 20, 12, 7), 0.0, 0.0)[0] > 89.5
    assert astro.sun_position(utc(2024, 6, 21, 0, 0), 51.4779, 0.0)[0] < -10


def test_sun_events_match_almanac():
    ev = astro.sun_events(utc(2024, 6, 21, 0, 0), 51.4779, 0.0)
    # HM Nautical Almanac / timeanddate for Greenwich: sunrise 03:43, sunset 20:21 UTC
    assert abs((ev["sunrise"] - utc(2024, 6, 21, 3, 43)).total_seconds()) < 120
    assert abs((ev["sunset"] - utc(2024, 6, 21, 20, 21)).total_seconds()) < 120
    assert ev["civil_dawn"] < ev["sunrise"] < ev["sunset"] < ev["civil_dusk"]
    # polar night: no sunrise within 30 h at 80 N in December
    assert astro.sun_events(utc(2024, 12, 21), 80.0, 0.0)["sunrise"] is None


@pytest.mark.parametrize("elev,phase", [(10, "day"), (-0.5, "day"), (-3, "civil twilight"),
                                        (-9, "nautical twilight"), (-15, "astronomical twilight"),
                                        (-30, "night")])
def test_light_phase(elev, phase):
    assert astro.light_phase(elev) == phase


def test_moon_phase_at_known_lunations():
    full = astro.moon_phase(utc(2024, 1, 25, 17, 54))     # full moon (USNO)
    new = astro.moon_phase(utc(2024, 1, 11, 11, 57))      # new moon
    first = astro.moon_phase(utc(2024, 1, 18, 3, 53))     # first quarter
    assert full["illumination"] > 0.99 and full["phase"] == "full moon"
    assert new["illumination"] < 0.01 and new["phase"] == "new moon"
    assert first["illumination"] == pytest.approx(0.5, abs=0.02) and first["phase"] == "first quarter"


def test_light_at_shape():
    L = astro.light_at(utc(2026, 11, 22, 23, 0), 32.11, 20.05)
    assert L["phase"] == "night" and 0 <= L["moon"]["illumination"] <= 1
    assert isinstance(L["moon"]["up"], bool) and -90 <= L["moon"]["elev_deg"] <= 90


# ------------------------------------------------------------------ passes

def test_sensor_models_and_picking():
    assert passes.sensor_for("SENTINEL-2C")["kind"] == "optical"
    assert passes.sensor_for("LANDSAT 9")["half_swath_km"] == 92.5
    assert passes.sensor_for("SENTINEL-1D")["kind"] == "sar"
    assert passes.sensor_for("SENTINEL-3A") is None and passes.sensor_for("ISS (ZARYA)") is None
    names = [r[0][1] for r in passes.pick_satellites(SAT_ROWS)]
    assert names == ["LANDSAT 8", "SENTINEL-1C", "SENTINEL-2B"]


def test_geometry_helpers():
    # Sentinel-1 IW at ~700 km: incidence 29.1 / 46 deg -> ~340 / ~610 km ground range
    assert passes.ground_range_km(29.1, 700) == pytest.approx(345, abs=15)
    assert passes.ground_range_km(46.0, 700) == pytest.approx(615, abs=20)
    assert passes.off_nadir_deg(0, 786) == 0
    assert passes.off_nadir_deg(145, 786) == pytest.approx(10.3, abs=0.4)  # MSI half FOV


def test_orbit_invariants_sentinel2():
    """SGP4 + TEME->ECEF + geodetic subpoint: the ground track reaches the orbit's
    inclination limit, the height is S2's ~786-818 km, and the descending node is near 10:30
    local solar time (sun-synchronous)."""
    sat = passes.satrec(SAT_ROWS["42063"])
    t0 = utc(2026, 11, 23).timestamp()
    ts = np.arange(t0, t0 + 6100, 5.0)  # one orbit (~100.6 min)
    p, bad = passes.ecef(sat, ts)
    assert not bad.any()
    lat, lon, h = passes.subpoint(p)
    assert np.abs(lat).max() == pytest.approx(180 - 98.5717, abs=0.3)
    assert 780 < h.min() and h.max() < 825  # height above the ellipsoid varies with latitude
    i = next(k for k in range(1, len(lat)) if lat[k - 1] > 0 >= lat[k])  # descending node
    t = datetime.fromtimestamp(ts[i], UTC)
    lst_h = (t.hour + t.minute / 60 + lon[i] / 15) % 24
    eot_h = astro._sun_ecliptic(astro.julian_day(t))[2] / 60
    assert 10.2 < (lst_h + eot_h) % 24 < 10.9


def test_predict_matches_brute_force_and_footprints():
    lat, lon = 47.76, 39.93
    start, end = utc(2026, 11, 20), utc(2026, 11, 27)
    rows = passes.pick_satellites(SAT_ROWS)
    ps = passes.predict(rows, lat, lon, start, end)
    assert ps and ps == sorted(ps, key=lambda p: p["t"])
    kinds = {p["kind"] for p in ps}
    assert kinds == {"optical", "sar"}
    for p in ps:
        if p["kind"] == "optical":
            half = 145.0 if p["mission"] == "Sentinel-2" else 92.5
            assert p["cross_km"] <= half and p["sun_elev_deg"] >= passes.MIN_SUN_ELEV
            assert p["node"] == "descending"  # day-side passes of these orbits
        else:
            assert p["side"] == "right" and 29.1 <= p["incidence_deg"] <= 46.0
        assert p["time_unc_s"] >= 1 and p["element_age_h"] >= 0
    # the refined closest approach equals a 1 s brute-force minimum within 1 km / 2 s
    p = ps[0]
    sat = passes.satrec(next(r for r in SAT_ROWS.values() if r[1] == p["satellite"]))
    tc = datetime.fromisoformat(p["t"]).timestamp()
    ts = np.arange(tc - 600, tc + 600, 1.0)
    la, lo, _ = passes.subpoint(passes.ecef(sat, ts)[0])
    d = passes.ground_km(lat, lon, la, lo)
    assert abs(d.min() - p["cross_km"]) < 1.0 and abs(ts[int(np.argmin(d))] - tc) <= 2


def test_predict_drops_night_optical_and_far_tracks():
    # a polar-night site: no optical pass can qualify (sun below 10 deg all day)
    ps = passes.predict(passes.pick_satellites(SAT_ROWS), 78.2, 15.6, utc(2026, 12, 1),
                        utc(2026, 12, 4))
    assert ps and all(p["kind"] == "sar" for p in ps)
    assert passes.predict([], 0, 0, utc(2026, 12, 1), utc(2026, 12, 2)) == []
    assert all(math.isfinite(p["cross_km"]) for p in ps)
