import base64
from datetime import UTC, datetime

import h3

from osint.signals import gnss, outages, radiation, satellites, spaceweather
from osint.signals.aviation import SquawkTracker, emergency_code

NOW = 1795407300.0


def ac(**kw):
    base = {"hex": "4cae9c", "type": "adsb_icao", "lat": 34.7, "lon": 33.0, "alt_baro": 30000,
            "version": 2, "nic": 8, "nac_p": 10, "seen_pos": 1.0}
    return {**base, **kw}


# ------------------------------------------------------------------ gnss


def test_nav_sample_classifies_integrity():
    assert not gnss.nav_sample(ac(), NOW).bad
    assert gnss.nav_sample(ac(nic=0), NOW).bad
    assert gnss.nav_sample(ac(nac_p=7), NOW).bad
    s = gnss.nav_sample(ac(gpsOkBefore=NOW - 60), NOW)
    assert s.bad and s.gps_lost
    assert not gnss.nav_sample(ac(gpsOkBefore=NOW - 3600), NOW).gps_lost  # long recovered


def test_nav_sample_skips_unusable_reports():
    assert gnss.nav_sample(ac(type="mlat"), NOW) is None  # ground-computed position
    assert gnss.nav_sample(ac(alt_baro="ground"), NOW) is None
    assert gnss.nav_sample(ac(alt_baro=500), NOW) is None
    assert gnss.nav_sample(ac(version=0), NOW) is None
    assert gnss.nav_sample(ac(seen_pos=120), NOW) is None
    assert gnss.nav_sample(ac(lat=None), NOW) is None
    assert gnss.nav_sample(ac(nic=None, nac_p=None), NOW) is None


def test_level_bands():
    assert gnss.level(100, 1) == "low"  # 1 %
    assert gnss.level(20, 1) == "medium"  # 5 %
    assert gnss.level(10, 1) == "medium"  # 10 %, a single aircraft never makes "high"
    assert gnss.level(10, 3) == "high"


def test_aggregator_live_window_and_closed_hours():
    agg = gnss.GnssAggregator({"min_aircraft": 2})
    t0 = 1795406400.0  # an hour boundary
    samples = [gnss.nav_sample(ac(hex="a1", nic=0), t0 + 60),
               gnss.nav_sample(ac(hex="a1", nic=8), t0 + 120),  # same aircraft: stays degraded
               gnss.nav_sample(ac(hex="b2", lat=34.71), t0 + 60),
               gnss.nav_sample(ac(hex="c3", lat=55.0, lon=20.0), t0 + 60)]
    agg.add(samples)
    cells = agg.snapshot(t0 + 300)
    assert len(cells) == 1  # the Baltic cell has one aircraft (< min_aircraft)
    c = cells[0]
    assert (c["n"], c["bad"], c["pct"]) == (2, 1, 50.0)
    assert c["h3"] == h3.latlng_to_cell(34.7, 33.0, 3) and len(c["b"]) >= 12
    assert agg.pop_closed_hours(t0 + 300) == []  # hour still open
    closed = agg.pop_closed_hours(t0 + 3700)
    assert sorted((n, bad) for _, _, n, bad in closed) == [(1, 0), (2, 1)]
    assert agg.snapshot(t0 + 60 + 3601) == []  # aged out of the 60 min window


def test_aggregator_ignores_degradation_carried_from_elsewhere():
    agg = gnss.GnssAggregator({"min_aircraft": 1})
    t0 = 1795406400.0
    # a long-haul arrival degraded for 40 min over ~550 km (GPS deselected after spoofing)
    agg.add([gnss.nav_sample(ac(hex="a350", nic=0, nac_p=0, lat=45.0 + i, lon=10.0),
                             t0 + i * 600) for i in range(5)])
    cells = {c["h3"]: c for c in agg.snapshot(t0 + 2400)}
    assert sum(c["bad"] for c in cells.values()) == 0
    assert sum(c["carried"] for c in cells.values()) == 5  # every cell it passed, retroactively
    assert sum(bad for *_, bad in agg.pop_closed_hours(t0 + 3600)) == 0
    # a jammed aircraft circling locally stays degraded
    agg.add([gnss.nav_sample(ac(hex="local", nic=0, lat=34.7, lon=33.0 + i * 0.05),
                             t0 + 3600 + i * 600) for i in range(5)])
    assert any(c["bad"] for c in agg.snapshot(t0 + 6000))
    # good navigation again ends the run
    agg.add([gnss.nav_sample(ac(hex="a350", lat=49.5, lon=10.0), t0 + 6000)])
    assert "a350" not in agg.carried


# ------------------------------------------------------------------ squawks


def test_emergency_code():
    assert emergency_code({"squawk": "7700"}) == "7700"
    assert emergency_code({"squawk": "1200", "emergency": "nordo"}) == "7600"
    assert emergency_code({"squawk": "1200", "emergency": "minfuel"}) == "minfuel"
    assert emergency_code({"squawk": "1200", "emergency": "lifeguard"}) is None
    assert emergency_code({"squawk": "1200", "emergency": "none"}) is None


def test_squawk_tracker_one_episode_per_flight():
    t = SquawkTracker(gap_s=1800)
    a = {"hex": "4CA123", "squawk": "7700", "flight": "RYR1 ", "lat": 50.0, "lon": 5.0,
         "alt_baro": 12000, "t": "B738", "seen_pos": 0}
    assert len(t.update([a], NOW)) == 1
    assert t.update([{**a, "lat": 50.1}], NOW + 120) == []  # same flight, same code
    items = t.items(NOW + 120)
    assert len(items) == 1 and items[0]["lat"] == 50.1 and items[0]["active"]
    assert items[0]["first_lat"] == 50.0 and items[0]["type"] == "B738"
    assert len(t.update([a], NOW + 120 + 3600)) == 1  # reappears after the gap: new episode
    assert len(t.update([{**a, "squawk": "7600"}], NOW + 4000)) == 1  # other code
    restored = SquawkTracker()
    restored.load_cursor(t.to_cursor())
    assert restored.update([a], NOW + 4000) == []  # restart does not re-announce
    t.prune(NOW + 4000 + 86401)
    assert t.items(NOW) == []


# ------------------------------------------------------------------ satellites

OMM = {"OBJECT_NAME": "ISS (ZARYA)", "OBJECT_ID": "1998-067A", "EPOCH": "2026-11-22T21:08:45",
       "MEAN_MOTION": 15.49, "ECCENTRICITY": 0.0007, "INCLINATION": 51.63,
       "RA_OF_ASC_NODE": 234.76, "ARG_OF_PERICENTER": 54.9, "MEAN_ANOMALY": 305.2,
       "NORAD_CAT_ID": 25544, "BSTAR": 0.00013, "MEAN_MOTION_DOT": 7e-5}


def test_compact_and_select():
    row = satellites.compact(OMM)
    assert row[:4] == [25544, "ISS (ZARYA)", "1998-067A", "2026-11-22T21:08:45"]
    assert row[-1] == 0.0  # missing MEAN_MOTION_DDOT
    assert satellites.compact({**OMM, "MEAN_MOTION": None}) is None
    assert satellites.parse_group("GP data has not updated") == []
    star = satellites.compact({**OMM, "NORAD_CAT_ID": 44713, "OBJECT_NAME": "STARLINK-1007"})
    assert satellites.derive([row, star], {"starlink": "STARLINK"}) == {"starlink": [44713]}
    cache = {"objects": {"25544": row, "44713": star},
             "membership": {"active": [25544, 44713], "stations": [25544],
                            "visual": [25544], "starlink": [44713]},
             "groups": {"stations": {"count": 1}}}
    out = satellites.select(cache, ["stations"], 10)
    assert [r[0] for r in out["items"]] == [25544]
    assert out["items"][0][-1] == ["stations", "visual"]  # every non-"active" group
    assert satellites.select(cache, ["stations", "starlink"], 1)["truncated"]
    assert satellites.select(None, ["stations"], 10)["items"] == []


# ------------------------------------------------------------------ outages

IODA = {"data": [
    {"entity": {"code": "ir", "name": "Iran"}, "from": 1000, "until": int(NOW) - 60,
     "score": 50000, "datasource": "bgp", "method": "median"},
    {"entity": {"code": "IR", "name": "Iran"}, "from": 2000, "until": 3000,
     "score": 90000, "datasource": "merit-nt", "method": "median"},
    {"entity": {"code": "NL", "name": "Netherlands"}, "from": 2000, "until": int(NOW),
     "score": 135000, "datasource": "merit-nt", "method": "median"},
    {"entity": {"code": "XK", "name": "Kosovo"}, "from": 1, "until": 2, "score": 55,
     "datasource": "bgp"},
    {"entity": {}, "from": 1, "until": 2, "score": 5000, "datasource": "bgp"},
]}


def test_ioda_parse_and_rollup():
    ev = outages.parse_events(IODA, NOW)
    assert [(e["cc"], e["datasource"]) for e in ev] == [
        ("IR", "bgp"), ("IR", "merit-nt"), ("NL", "merit-nt")]  # low score / no code dropped
    assert ev[0]["ongoing"] and not ev[1]["ongoing"]
    assert ev[0]["severity"] > ev[2]["severity"]  # bgp outweighs a bigger telescope score
    assert outages.notable(ev[0]) and not outages.notable(ev[2])
    by = outages.by_country(ev)
    assert by[0]["cc"] == "IR" and by[0]["datasources"] == ["bgp", "merit-nt"]
    assert by[0]["events"] == 2 and by[0]["level"] == "high"
    assert outages.severity(0, 1.0) == 0.0


# ------------------------------------------------------------------ space weather


def test_swpc_parsers():
    kp = spaceweather.parse_kp([{"time_tag": "2026-11-23T00:00:00", "Kp": 5.33},
                                {"time_tag": "bad", "Kp": 1}])
    assert kp == [[datetime(2026, 11, 23, tzinfo=UTC).timestamp(), 5.33]]
    one = spaceweather.parse_kp([{"time_tag": "2026-11-23T04:15:00", "kp_index": 2,
                                  "estimated_kp": 2.33}])
    assert one[0][1] == 2.33
    assert spaceweather.parse_kp([["time_tag", "Kp"], ["2026-11-23 00:00:00", "4.00"]])[0][1] == 4
    assert [spaceweather.g_scale(k) for k in (4.67, 5.0, 7.33, 9.0)] == [0, 1, 3, 5]
    scales = spaceweather.parse_scales({"0": {"G": {"Scale": "2", "Text": "moderate"},
                                              "S": {"Scale": "0"}, "R": {"Scale": None}}})
    assert scales["now"]["G"] == 2 and scales["now"]["R"] is None
    msg = ("Space Weather Message Code: ALTK06\r\nSerial Number: 1\r\nIssue Time: x\r\n\r\n"
           "ALERT: Geomagnetic K-index of 6\nThreshold Reached: 2026 Nov 19\n"
           "NOAA Scale: G2 - Moderate\n")
    a = spaceweather.parse_alerts([{"product_id": "K06A", "issue_datetime":
                                    "2026-11-19 01:08:54.450", "message": msg}])[0]
    assert a["kind"] == "ALERT" and a["headline"] == "Geomagnetic K-index of 6"
    assert a["scales"] == {"G": 2} and a["notable"]
    k4 = msg.replace("G2 - Moderate", "G0").replace("of 6", "of 4")
    assert not spaceweather.parse_alert({"product_id": "K04A", "issue_datetime":
                                         "2026-11-19 01:00:00", "message": k4})["notable"]


def test_aurora_grid():
    g = spaceweather.aurora_grid({"Observation Time": "t", "coordinates": [
        [0, -90, 1], [359, 90, 55], [10, 65, 120], ["x", 1, 2]]})
    raw = base64.b64decode(g["b64"])
    assert len(raw) == 360 * 181 and g["max"] == 100
    assert raw[0] == 1 and raw[180 * 360 + 359] == 55 and raw[155 * 360 + 10] == 100
    assert spaceweather.aurora_grid({}) is None


# ------------------------------------------------------------------ radiation


def test_radiation_parsers_and_assess():
    bfs = radiation.parse_bfs({"features": [
        {"geometry": {"coordinates": [10.13, 47.64]}, "properties": {
            "id": "DEZ1874", "name": "Weitnau", "site_status": 1,
            "end_measure": "2026-11-23T03:00:00Z", "value": 0.112, "value_cosmic": 0.054}},
        {"geometry": {"coordinates": [10, 47]}, "properties": {
            "id": "DEZ0001", "site_status": 3, "end_measure": "2026-11-23T03:00:00Z",
            "value": 0.1}}]})
    assert [r["id"] for r in bfs] == ["bfs:DEZ1874"] and bfs[0]["value"] == 0.112
    now = datetime(2026, 11, 23, 4, tzinfo=UTC)
    sc = radiation.parse_safecast([
        {"device": 1, "when_captured": "2026-11-23T03:30:00Z", "loc_lat": 37.7,
         "loc_lon": 140.7, "lnd_7318c": 33.4, "loc_country": "JP"},
        {"device": 2, "when_captured": "2012-00-00T00:00:00Z", "loc_lat": 1, "loc_lon": 1,
         "lnd_7318u": 50},
        {"device": 3, "when_captured": "2026-11-23T03:30:00Z", "loc_lat": 1, "loc_lon": 1,
         "lnd_712u": 0}], now, 6)
    assert [(r["id"], r["value"]) for r in sc] == [("safecast:1", 0.1)]
    assert radiation.assess(0.3, None) == "unknown"
    assert radiation.assess(3.0, [2.8, 100]) == "normal"  # contaminated site, own baseline
    assert radiation.assess(0.3, [0.1, 100]) == "elevated"
    assert radiation.assess(1.2, [0.1, 100]) == "alert"
    assert radiation.assess(0.18, [0.08, 100]) == "normal"  # below floor
    assert radiation.assess(0.3, [0.1, 3]) == "unknown"  # too little history
