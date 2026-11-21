import math
from datetime import UTC, datetime, timedelta

import pytest

from osint.config import ROOT
from osint.core.metrics import Metrics
from osint.core.models import Observation, Position, TrackInfo
from osint.pipeline.sensor_events import (
    SensorEventDetector,
    SensorEventsConfig,
    orbit_turn,
)
from osint.store.lake import Lake, observation_row, position_row
from osint.store.sqlite import SqliteStore

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
BASE = (33.0, 44.0)


def offset(lat, lon, km_north=0.0, km_east=0.0):
    return (lat + km_north / 111.2,
            lon + km_east / (111.2 * math.cos(math.radians(lat))))


def racetrack(d, leg=50.0, r=10.0):
    """(east, north) km on a stadium-shaped racetrack at arc length d."""
    per = 2 * leg + 2 * math.pi * r
    d %= per
    if d < leg:
        return d, 0.0
    d -= leg
    if d < math.pi * r:
        a = -math.pi / 2 + d / r
        return leg + r * math.cos(a), r + r * math.sin(a)
    d -= math.pi * r
    if d < leg:
        return leg - d, 2 * r
    a = math.pi / 2 + (d - leg) / r
    return r * math.cos(a), r + r * math.sin(a)


def apos(track, where, ts, alt=25000, kind="air"):
    return position_row(Position(track_id=track, kind=kind, ts=ts, lat=where[0], lon=where[1],
                                 source="t", alt_ft=alt if kind == "air" else None))


@pytest.fixture
def env(tmp_path):
    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    m = Metrics()
    det = SensorEventDetector(store, lake, m, SensorEventsConfig.from_file(
        ROOT / "config" / "corroboration.yaml"), clock=lambda: NOW)
    yield lake, store, det, m
    store.close()
    lake.close()


def sensor_events(store, detector=None):
    out = store.list_events(limit=100)
    summ = store.corroboration_summaries([e["id"] for e in out])
    return [e for e in out if detector is None or summ.get(e["id"], {}).get(
        "detector") == detector]


# ----------------------------------------------------------------------- orbit


def test_orbit_turn_racetrack_vs_straight():
    t0 = NOW - timedelta(minutes=90)
    track = [(t0 + timedelta(minutes=i), *offset(*BASE, *reversed(racetrack(12.3 * i))),
              None) for i in range(90)]
    straight = [(t0 + timedelta(minutes=i), *offset(*BASE, km_east=12.3 * i), None)
                for i in range(90)]
    w, g = timedelta(minutes=60), timedelta(minutes=10)
    assert orbit_turn(track, w, g)[0] >= 1080
    assert orbit_turn(straight, w, g)[0] < 30
    # reported headings take precedence over computed bearings
    zigzag = [(t0 + timedelta(minutes=i), *BASE, (i % 2) * 90.0) for i in range(30)]
    assert orbit_turn(zigzag, w, g)[0] == pytest.approx(29 * 90)
    # turns across a data gap are not counted
    gappy = [(t0 + timedelta(minutes=20 * i), *BASE, (i % 2) * 90.0) for i in range(4)]
    assert orbit_turn(gappy, w, g)[0] == 0


def test_orbit_detector_tanker_racetrack_not_straight_or_civil(env):
    lake, store, det, m = env
    store.upsert_tracks([
        TrackInfo(track_id="air:t1", kind="air", ident="t1", type_code="K35R", name="QID61",
                  is_mil=True, high_priority=True),
        TrackInfo(track_id="air:t2", kind="air", ident="t2", type_code="K35R", is_mil=True),
        TrackInfo(track_id="air:c1", kind="air", ident="c1", type_code="A332", is_mil=False),
        TrackInfo(track_id="air:f1", kind="air", ident="f1", type_code="F16", is_mil=True),
    ], NOW)
    t0 = NOW - timedelta(minutes=90)
    rows = []
    for i in range(90):
        ts = t0 + timedelta(minutes=i)
        e, n = racetrack(12.3 * i)
        rows.append(apos("air:t1", offset(*BASE, n, e), ts))
        rows.append(apos("air:c1", offset(*BASE, n, e), ts))  # civil airliner: ignored
        rows.append(apos("air:f1", offset(*BASE, n, e), ts))  # fighter type: not an orbiter
        rows.append(apos("air:t2", offset(*BASE, 300, 12.3 * i), ts))  # straight transit
    lake.write("positions", rows)
    out = det.run_once()
    assert out["created"] == {"orbit": 1}
    [ev] = sensor_events(store, "orbit")
    assert ev["category"] == "air_activity" and ev["status"] == "sensor-corroborated"
    assert ev["title"].startswith("Military orbit: K35R QID61") and ev["precision"] == "point"
    assert 0.5 <= ev["confidence"] <= 0.95
    [c] = store.list_corroborations(ev["id"])
    assert c["kind"] == "aircraft" and c["ref_id"] == "air:t1" and c["rule"] == "orbit"
    assert c["details"]["turn_deg"] >= 540
    assert m.get("sensor_events_created_total", detector="orbit") == 1
    # idempotent: the same pass again changes nothing
    again = det.run_once()
    assert again["created"] == {} and again["updated"] == {}
    assert len(sensor_events(store, "orbit")) == 1


# --------------------------------------------------------------- concentration


def _fleet(n, ts, prefix="m", where=BASE):
    return [apos(f"air:{prefix}{i}", offset(*where, km_north=5 * i), ts) for i in range(n)]


def test_concentration_above_baseline_only(env):
    lake, store, det, _ = env
    other = (10.0, 10.0)
    store.upsert_tracks([TrackInfo(track_id=f"air:{p}{i}", kind="air", ident=f"{p}{i}",
                                   type_code="C17", is_mil=True)
                         for p in ("m", "b") for i in range(10)], NOW)
    rows = _fleet(9, NOW - timedelta(minutes=10))  # 9 aircraft now at BASE, none before
    # at `other` the same 9 aircraft are there every 30 min for two days: that is normal
    for k in range(1, 96):
        rows += _fleet(9, NOW - timedelta(minutes=30 * k + 5), "b", other)
    rows += _fleet(9, NOW - timedelta(minutes=10), "b", other)
    rows += [apos("air:m9", BASE, NOW - timedelta(minutes=5), alt=0)]  # parked: ignored
    lake.write("positions", rows)
    out = det.run_once()
    assert out["created"] == {"concentration": 1}
    [ev] = sensor_events(store, "concentration")
    assert ev["title"] == "Military aircraft concentration: 9 aircraft"
    assert abs(ev["lat"] - BASE[0]) < 1
    assert len(store.list_corroborations(ev["id"])) == 9


def test_concentration_extends_instead_of_duplicating(env):
    lake, store, det, _ = env
    store.upsert_tracks([TrackInfo(track_id=f"air:m{i}", kind="air", ident=f"m{i}",
                                   type_code="C17", is_mil=True) for i in range(8)], NOW)
    lake.write("positions", _fleet(8, NOW - timedelta(minutes=10)))
    det.run_once()
    later = NOW + timedelta(minutes=5)
    det.clock = lambda: later
    lake.write("positions", _fleet(8, later - timedelta(minutes=2)))
    out = det.run_once()
    assert out["created"] == {} and out["updated"] == {"concentration": 1}
    [ev] = sensor_events(store, "concentration")
    full = store.get_event(ev["id"])
    assert full["version"] == 2 and full["t_end"].startswith("2026-11-21T12:05")


# --------------------------------------------------------------------- AIS gap


def test_ais_gap_needs_coverage(env):
    lake, store, det, _ = env
    sea = (26.5, 56.3)
    store.upsert_tracks([TrackInfo(track_id="sea:1", kind="sea", ident="1", name="DARK ONE"),
                         TrackInfo(track_id="sea:2", kind="sea", ident="2")], NOW)
    rows = [apos("sea:1", sea, NOW - timedelta(hours=20), kind="sea"),
            apos("sea:1", offset(*sea, 20), NOW - timedelta(hours=11), kind="sea")]  # 9 h dark
    lake.write("positions", rows)
    assert det.run_once()["created"] == {}  # nobody else reported there: coverage hole
    lake.write("positions", [apos("sea:2", offset(*sea, 30, 30), NOW - timedelta(hours=15),
                                  kind="sea")])
    assert det.run_once()["created"] == {"ais_gap": 1}
    [ev] = sensor_events(store, "ais_gap")
    assert ev["category"] == "maritime_security" and "DARK ONE" in ev["title"]
    [c] = store.list_corroborations(ev["id"])
    assert c["kind"] == "ais_gap" and c["details"]["gap_h"] == 9.0
    assert c["ref_id"].startswith("sea:1@")


def test_ais_gap_rejects_impossible_jump(env):
    lake, _store, det, _ = env
    sea = (26.5, 56.3)
    lake.write("positions", [
        apos("sea:1", sea, NOW - timedelta(hours=20), kind="sea"),
        apos("sea:1", offset(*sea, 1000), NOW - timedelta(hours=12), kind="sea"),  # 67 kn
        apos("sea:2", sea, NOW - timedelta(hours=15), kind="sea")])
    assert det.run_once()["created"] == {}


# --------------------------------------------------------------------- seismic


def test_seismic_explosion_events_and_idempotency(env):
    lake, store, det, m = env
    obs = [Observation(id="quake:x1", type="quake", ts=NOW - timedelta(hours=3), lat=40.1,
                       lon=-112.1, source="usgs", value=1.8,
                       attrs={"event_type": "explosion", "place": "10 km N of Mine"}),
           Observation(id="quake:x2", type="quake", ts=NOW - timedelta(hours=2), lat=40.1,
                       lon=-112.1, source="usgs", value=1.5,
                       attrs={"event_type": "quarry blast"}),
           Observation(id="quake:e1", type="quake", ts=NOW - timedelta(hours=1), lat=35.0,
                       lon=139.0, source="usgs", value=4.0,
                       attrs={"event_type": "earthquake"})]
    lake.write("observations", [observation_row(o) for o in obs])
    assert det.run_once()["created"] == {"seismic": 2}
    evs = {e["title"]: e for e in sensor_events(store, "seismic")}
    assert set(evs) == {"Seismic explosion: M1.8 10 km N of Mine",
                        "Seismic quarry blast: M1.5 40.10, -112.10"}
    assert evs["Seismic explosion: M1.8 10 km N of Mine"]["confidence"] == 0.8
    assert det.run_once()["created"] == {}
    assert len(sensor_events(store, "seismic")) == 2
    stats = store.corroboration_stats()
    assert stats["sensor_derived"] == {"total": 2, "by_detector": {"seismic": 2}}
    assert m.get("corroborations_total", kind="quake") == 2


def test_event_builder_does_not_load_sensor_events(env, tmp_path):
    from osint.pipeline.events import EventBuilder, EventsConfig

    lake, store, det, _ = env
    lake.write("observations", [observation_row(Observation(
        id="quake:x1", type="quake", ts=NOW - timedelta(hours=1), lat=40.1, lon=-112.1,
        source="usgs", value=1.8, attrs={"event_type": "explosion"}))])
    det.run_once()
    b = EventBuilder(store, lake, Metrics(), EventsConfig(), clock=lambda: NOW)
    b.warm_start()
    assert b.clusterer.events == {}
    assert len(store.list_events(limit=10)) == 1  # and the migration kept it


def test_wire_sets_runtime_attribute(tmp_path):
    from osint.config import Settings
    from osint.pipeline.sensor_events import wire
    from osint.runtime import Runtime

    rt = Runtime(Settings(_env_file=None, data_dir=tmp_path), [])
    d = wire(rt)
    assert rt.sensor_events is d and d.store is rt.store
    rt.store.close()
