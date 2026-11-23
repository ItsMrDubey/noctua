import gzip
import json
import math
from datetime import UTC, datetime, timedelta

from osint.profiles import actypes
from osint.profiles import behaviour as bh

T0 = datetime(2026, 11, 23, 0, 0, tzinfo=UTC)


def pt(minutes, lat, lon, alt=None, spd=None):
    return {"ts": (T0 + timedelta(minutes=minutes)).isoformat(), "lat": lat, "lon": lon,
            "alt_ft": alt, "speed_kts": spd, "heading_deg": None}


def racetrack(n=120, lat0=44.0, lon0=31.0, r_deg=0.3):
    return [pt(i * 3, lat0 + r_deg * math.sin(i / 6), lon0 + r_deg * math.cos(i / 6), 28000, 420)
            for i in range(n)]


def test_summarize_and_spark():
    s = bh.summarize(racetrack())
    assert s["fixes"] == 120 and s["max_alt_ft"] == 28000 and s["distance_km"] > 300
    assert len(s["spark"]["alt_ft"]) == bh.SPARK_N
    assert bh.summarize([]) is None


def test_orbit_detects_racetracks_not_transits():
    o = bh.orbit(racetrack())
    assert o and o["radius_km"] < 60 and o["path_km"] > 300 and o["laps"] > 1
    transit = [pt(i * 5, 50 + i * 0.1, 10 + i * 0.1, 35000, 450) for i in range(60)]
    assert bh.orbit(transit) is None


def test_gaps_and_jumps():
    pts = [pt(0, 57.0, 11.0, spd=11), pt(30, 57.05, 11.0, spd=11), pt(30 + 14 * 60, 58.5, 11.0, spd=11),
           pt(30 + 14 * 60 + 10, 59.5, 11.0, spd=11)]
    g = bh.gaps(pts, 6)
    assert len(g) == 1 and g[0]["hours"] == 14.0 and g[0]["jump_km"] > 150
    j = bh.jumps(pts, 50)
    assert len(j) == 1 and j[0]["implied_kts"] > 300  # 111 km in 10 minutes


def test_stops_name_the_nearest_port():
    pts = [pt(i * 20, 59.9 + 0.0001 * i, 30.2, spd=0.2) for i in range(10)] + [pt(300, 60.2, 29.5, spd=12)]
    ports = [{"name": "Saint Petersburg", "country": "RU", "lat": 59.93, "lon": 30.22},
             {"name": "Far", "country": "FI", "lat": 60.5, "lon": 25.0}]
    s = bh.stops(pts, ports)
    assert len(s) == 1 and s[0]["port"] == "Saint Petersburg" and s[0]["port_km"] < 5
    assert bh.stops(pts, []) [0]["port"] is None


def test_events_near_respects_time_and_distance():
    pts = racetrack()
    evs = [{"id": "e1", "title": "Strike", "lat": 44.0, "lon": 31.3, "t_start": pts[10]["ts"], "category": "conflict"},
           {"id": "e2", "title": "Too late", "lat": 44.0, "lon": 31.3, "t_start": (T0 + timedelta(days=3)).isoformat()},
           {"id": "e3", "title": "Too far", "lat": 40.0, "lon": 20.0, "t_start": pts[10]["ts"]},
           {"id": "e4", "title": "No place", "lat": None, "lon": None}]
    got = bh.events_near(pts, evs, 30)
    assert [e["id"] for e in got] == ["e1"] and got[0]["km"] < 5


def test_frontline_distance():
    line = [[(47.0, 36.0), (48.0, 37.0)]]
    pts = [pt(0, 47.5, 35.0), pt(10, 47.5, 36.4)]
    d = bh.frontline_distance(pts, line)
    assert d and d["km"] < 10
    assert bh.frontline_distance([pt(0, 55, 10)], line) is None


def test_gnss_hits_only_inside_the_snapshot_window():
    import h3

    cell = h3.latlng_to_cell(55.0, 20.0, 3)
    snap = {"res": 3, "window_min": 60, "updated": (T0 + timedelta(minutes=100)).timestamp(),
            "cells": [{"h3": cell, "level": "high", "pct": 40.0, "n": 10, "bad": 4, "lat": 55, "lon": 20}]}
    assert bh.gnss_hits([pt(90, 55.0, 20.0)], snap)[0]["level"] == "high"
    assert bh.gnss_hits([pt(0, 55.0, 20.0)], snap) == []  # before the window


def test_squawk_history_filters_by_hex():
    snap = {"items": [{"hex": "3c55c7", "code": "7700", "label": "General emergency", "flight": "EWG4HV",
                       "first_seen": T0.timestamp(), "last_seen": T0.timestamp() + 60, "active": False},
                      {"hex": "aaaaaa", "code": "7600"}]}
    got = bh.squawk_history(snap, "3c55c7")
    assert len(got) == 1 and got[0]["code"] == "7700" and got[0]["first_seen"].startswith("2026-11-23T00:00")


def test_watch_alerts_reads_one_track(tmp_path):
    from osint.infra.job import WatchStore

    st = WatchStore(tmp_path / "w.sqlite")
    st.add_alert({"ts": "2026-11-23T01:00:00+00:00", "kind": "proximity", "track_id": "sea:1", "severity": "warn",
                  "title": "Loitering near cable", "sub": "2 kt within 1.2 km of C-Lion1", "detail": {"km": 1.2}})
    st.add_alert({"ts": "2026-11-23T01:00:00+00:00", "kind": "proximity", "track_id": "sea:2", "severity": "warn",
                  "title": "x"})
    st.close()
    got = bh.watch_alerts(tmp_path / "w.sqlite", "sea:1")
    assert len(got) == 1 and got[0]["detail"] == {"km": 1.2}
    assert bh.watch_alerts(tmp_path / "missing.sqlite", "sea:1") == []


def test_aircraft_types_parse_and_describe(tmp_path):
    raw = gzip.compress(json.dumps({"R135": ["BOEING RC-135", "L4J", "H"], "H60": ["SIKORSKY UH-60", "H2T", "M"]}).encode())
    table = actypes.parse(raw)
    d = actypes.describe("r135", table)
    assert d == {"designator": "R135", "name": "BOEING RC-135", "desc": "L4J", "wtc": "Heavy",
                 "class": "Landplane", "engines": 4, "engine_type": "jet"}
    assert actypes.describe("H60", table)["class"] == "Helicopter"
    assert actypes.describe("ZZZZ", table) is None and actypes.describe(None, table) is None
    (tmp_path / actypes.FILE).write_text(json.dumps(table))
    assert actypes.TypeTable(tmp_path / actypes.FILE).describe("H60")["engine_type"] == "turboprop / turboshaft"


def test_events_around_drops_the_assets_own_events():
    from types import SimpleNamespace

    pts = racetrack()
    evs = [{"id": "self", "title": "Military orbit: R135 RRR7223", "lat": 44.0, "lon": 31.3, "t_start": pts[5]["ts"]},
           {"id": "other", "title": "Explosion reported", "lat": 44.0, "lon": 31.3, "t_start": pts[5]["ts"]}]
    rt = SimpleNamespace(store=SimpleNamespace(list_events=lambda **kw: evs))
    assert [e["id"] for e in bh.events_around(rt, pts, 30, 24, ("RRR7223", "ZZ664"))] == ["other"]
