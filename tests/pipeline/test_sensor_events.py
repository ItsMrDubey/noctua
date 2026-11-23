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
        ROOT / "config" / "corroboration.yaml"), clock=lambda: NOW, airports=[], bases=[])
    yield lake, store, det, m
    store.close()
    lake.close()


def history(ids_where, days=4, n=1, prefix="h"):
    """Baseline: ``n`` aircraft at each place in both 30-min buckets of the 11:00 hour (the
    hour of the 11:30-12:00 detection window) on each of ``days`` previous days."""
    rows = []
    for d in range(1, days + 1):
        for minute in (10, 40):
            ts = NOW.replace(hour=11, minute=minute) - timedelta(days=d)
            for w, where in enumerate(ids_where):
                rows += [apos(f"air:{prefix}{w}_{d}_{minute}_{i}", where, ts) for i in range(n)]
    return rows


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
    rows = _fleet(9, NOW - timedelta(minutes=10))  # 9 aircraft now at BASE, little before
    rows += history([BASE])
    # at `other` the same 9 aircraft are there every 30 min for four days: that is normal
    for k in range(1, 192):
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
    lake.write("positions", _fleet(8, NOW - timedelta(minutes=10)) + history([BASE]))
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


# ---------------------------------------------------------------------- hazard


def hazard(oid, source, hz, lat, lon, *, alert=None, hours_ago=2, value=None, **attrs):
    ts = NOW - timedelta(hours=hours_ago)
    return Observation(id=oid, type="hazard", ts=ts, lat=lat, lon=lon, source=source,
                       value=value, version="1", attrs={
                           "source": source, "hazard": hz, "alert_level": alert,
                           "name": attrs.pop("name", f"{hz} {oid}"),
                           "t_start": attrs.pop("t_start", (ts - timedelta(hours=6)).isoformat()),
                           "t_end": ts.isoformat(), "precision": "point", **attrs})


def hazard_events(store):
    out = {}
    for e in store.list_events(limit=100):
        full = store.get_event(e["id"])
        full["evidence"] = store.list_corroborations(e["id"])
        out[full["title"]] = full
    return out


def test_hazard_events_grouped_across_sources_and_filtered(env):
    lake, store, det, m = env
    obs = [
        hazard("hazard:gdacs:FL:1", "gdacs", "flood", -33.10, -71.67, alert="orange",
               name="Flood in Chile", country="Chile", iso2=["CL"], iso3="CHL",
               severity_text="Magnitude 0"),
        hazard("hazard:eonet:EONET_9", "eonet", "flood", -33.5, -71.2, name="Floods Chile"),
        # same type, far away: its own event
        hazard("hazard:eonet:EONET_10", "eonet", "flood", 10.0, 10.0, name="Floods Nigeria"),
        # below min_alert (earthquake: orange) and an excluded type: no events
        hazard("hazard:gdacs:EQ:2", "gdacs", "earthquake", -13.5, 172.2, alert="green",
               value=4.9),
        hazard("hazard:eonet:EONET_11", "eonet", "sea_lake_ice", -57.0, -23.0),
    ]
    lake.write("observations", [observation_row(o) for o in obs])
    assert det.run_once()["created"] == {"hazard": 2}
    evs = hazard_events(store)
    chile = evs["Flood in Chile"]
    assert chile["category"] == "disaster" and chile["subcategory"] == "flood"
    assert chile["status"] == "official" and chile["confidence"] == 0.9  # orange + 1 source
    assert chile["place_name"] == "Chile" and chile["country"] == "CL"
    assert (chile["lat"], chile["lon"]) == (-33.10, -71.67)
    assert {r["ref_id"] for r in chile["evidence"]} == {"hazard:gdacs:FL:1",
                                                        "hazard:eonet:EONET_9"}
    assert "Orange alert" in chile["summary"] and "GDACS, NASA EONET" in chile["summary"]
    assert evs["Floods Nigeria"]["confidence"] == 0.6
    assert det.run_once()["created"] == {}  # idempotent
    assert store.corroboration_stats()["sensor_derived"]["by_detector"] == {"hazard_flood": 2}
    assert m.get("corroborations_total", kind="hazard") == 3


def test_hazard_quake_deduped_against_usgs(env):
    lake, store, det, _ = env
    t0 = NOW - timedelta(hours=3)
    usgs = Observation(id="quake:us7000xyz", type="quake", ts=t0, lat=-6.10, lon=130.20,
                       source="usgs", value=6.4,
                       attrs={"event_type": "earthquake", "place": "Banda Sea",
                              "url": "https://earthquake.usgs.gov/x"})
    gd = hazard("hazard:gdacs:EQ:7", "gdacs", "earthquake", -6.3, 130.4, alert="orange",
                value=6.3, t_start=(t0 + timedelta(minutes=2)).isoformat(),
                name="Earthquake in Banda Sea")
    far = hazard("hazard:gdacs:EQ:8", "gdacs", "earthquake", 40.0, 20.0, alert="red",
                 value=6.0, name="Earthquake in Albania")  # no USGS match
    lake.write("observations", [observation_row(o) for o in (usgs, gd, far)])
    assert det.run_once()["created"] == {"hazard": 2}
    evs = hazard_events(store)
    banda = evs["Earthquake in Banda Sea"]
    assert (banda["lat"], banda["lon"]) == (-6.10, 130.20)  # placed on the USGS event
    assert banda["place_name"] == "Banda Sea" and banda["confidence"] == 0.9
    assert {(r["kind"], r["ref_id"]) for r in banda["evidence"]} == {
        ("hazard", "hazard:gdacs:EQ:7"), ("quake", "quake:us7000xyz")}
    assert store.get_sensor_event_by_key("hazard:quake:us7000xyz") is not None
    assert evs["Earthquake in Albania"]["confidence"] == 0.95
    # a later EONET record of the same quake joins the same event, not a new one
    lake.write("observations", [observation_row(hazard(
        "hazard:eonet:EONET_77", "eonet", "earthquake", -6.2, 130.3, value=6.4,
        t_start=(t0 + timedelta(minutes=1)).isoformat(), name="M 6.4 Banda Sea"))])
    assert det.run_once()["created"] == {}
    assert len(hazard_events(store)) == 2


def test_hazard_country_level_record_merges_by_country(env):
    lake, store, det, _ = env
    gd = hazard("hazard:gdacs:TC:5", "gdacs", "tropical_cyclone", 14.0, 125.0,
                alert="red", name="Tropical Cyclone FUNG-26", iso3="PHL", iso3s=["PHL"])
    rw = hazard("hazard:reliefweb:52371", "reliefweb", "tropical_cyclone", 11.75, 122.88,
                name="Philippines: Typhoon Fung - Nov 2026", iso3="PHL", iso3s=["PHL"],
                precision="country", country="Philippines")
    other = hazard("hazard:reliefweb:52372", "reliefweb", "flood", 9.0, 8.0,
                   name="Nigeria: Floods - Nov 2026", iso3="NGA", precision="country",
                   country="Nigeria")
    lake.write("observations", [observation_row(o) for o in (gd, rw, other)])
    assert det.run_once()["created"] == {"hazard": 2}
    evs = hazard_events(store)
    tc = evs["Tropical Cyclone FUNG-26"]
    assert len(tc["evidence"]) == 2 and tc["precision"] == "point"
    assert evs["Nigeria: Floods - Nov 2026"]["precision"] == "country"


def test_hazard_moving_storm_extends_its_event(env):
    lake, store, det, _ = env
    lake.write("observations", [observation_row(hazard(
        "hazard:gdacs:TC:9", "gdacs", "tropical_cyclone", 15.0, 130.0, alert="green",
        name="Tropical Cyclone A", hours_ago=10))])
    assert det.run_once()["created"] == {"hazard": 1}
    # the next advisory (new version) has moved ~250 km: same id -> same event
    moved = hazard("hazard:gdacs:TC:9", "gdacs", "tropical_cyclone", 16.5, 128.0,
                   alert="orange", name="Tropical Cyclone A", hours_ago=1)
    lake.write("observations", [observation_row(moved)])
    out = det.run_once()
    assert out["created"] == {} and out["updated"] == {"hazard": 1}
    ev = next(iter(hazard_events(store).values()))
    assert ev["confidence"] == 0.85 and "Orange alert" in ev["summary"]


def test_wire_sets_runtime_attribute(tmp_path):
    from osint.config import Settings
    from osint.pipeline.sensor_events import wire
    from osint.runtime import Runtime

    rt = Runtime(Settings(_env_file=None, data_dir=tmp_path), [])
    d = wire(rt)
    assert rt.sensor_events is d and d.store is rt.store
    rt.store.close()


# ------------------------------------------- false-positive guards (airports, trainers)

DXB = (25.25, 55.36)


def sites(det, airports=(), bases=()):
    det._airports, det._bases, det._sites_cache = list(airports), list(bases), None


def airport(where, typ="large_airport", mil=False):
    return {"lat": where[0], "lon": where[1], "type": typ, "is_military": mil}


def _fly(lake, track, centre, minutes, alt, leg=30.0, r=8.0, start_min=90):
    t0 = NOW - timedelta(minutes=start_min)
    lake.write("positions", [
        apos(track, offset(*centre, *reversed(racetrack(12.3 * i, leg, r))),
             t0 + timedelta(minutes=i), alt=alt) for i in range(minutes)])


def test_orbit_ignores_airport_holding_stack(env):
    lake, store, det, _ = env
    sites(det, [airport(DXB)])
    store.upsert_tracks([
        TrackInfo(track_id="air:e1", kind="air", ident="e1", type_code="A332",
                  name="UAE123", is_mil=False),
        TrackInfo(track_id="air:e2", kind="air", ident="e2", type_code="A332",
                  name="ZABEEL04", is_mil=True),  # misclassified airliner
        TrackInfo(track_id="air:k1", kind="air", ident="k1", type_code="K35R",
                  name="QID1", is_mil=True)], NOW)
    near = offset(*DXB, 15, 10)
    _fly(lake, "air:e1", near, 60, 8000)
    _fly(lake, "air:e2", near, 60, 8000)
    _fly(lake, "air:k1", near, 40, 8000, start_min=45)  # low, near airport, < 45 min
    assert det.run_once()["created"] == {}


def test_orbit_dual_use_type_needs_military_marker(env):
    lake, store, det, _ = env
    store.upsert_tracks([
        TrackInfo(track_id="air:e2", kind="air", ident="e2", type_code="A332",
                  name="ZABEEL04", is_mil=True),
        TrackInfo(track_id="air:m1", kind="air", ident="m1", type_code="A332",
                  name="MRTT12", is_mil=True)], NOW)
    _fly(lake, "air:e2", (10.0, 10.0), 90, 25000)
    assert det.run_once()["created"] == {}
    _fly(lake, "air:m1", (10.0, 10.0), 90, 25000)
    assert det.run_once()["created"] == {"orbit": 1}


def test_orbit_kc135_over_sea_far_from_airport(env):
    lake, store, det, _ = env
    sites(det, [airport(DXB)])
    store.upsert_tracks([TrackInfo(track_id="air:k1", kind="air", ident="k1",
                                   type_code="K35R", name="QID1", is_mil=True)], NOW)
    _fly(lake, "air:k1", offset(*DXB, km_east=200), 90, 25000)
    assert det.run_once()["created"] == {"orbit": 1}


def test_orbit_isr_low_near_home_base_for_an_hour_allowed(env):
    lake, store, det, _ = env
    sites(det, [airport(DXB)])
    store.upsert_tracks([TrackInfo(track_id="air:p1", kind="air", ident="p1",
                                   type_code="P8", name="PAT1", is_mil=True)], NOW)
    _fly(lake, "air:p1", offset(*DXB, 20), 90, 6000)
    assert det.run_once()["created"] == {"orbit": 1}


def test_orbit_too_small_for_aar_isr(env):
    lake, store, det, _ = env
    store.upsert_tracks([TrackInfo(track_id="air:p1", kind="air", ident="p1",
                                   type_code="P8", is_mil=True)], NOW)
    _fly(lake, "air:p1", (10.0, 10.0), 90, 25000, leg=5.0, r=4.0)  # ~ 14 km axis
    assert det.run_once()["created"] == {}


def _mil(store, prefix, n, typ, category=None):
    store.upsert_tracks([TrackInfo(track_id=f"air:{prefix}{i}", kind="air", ident=f"{prefix}{i}",
                                   type_code=typ, category=category, is_mil=True)
                         for i in range(n)], NOW)


def test_concentration_ignores_trainers(env):
    lake, store, det, _ = env
    cc = (27.7, -97.4)
    _mil(store, "t", 4, "T6", "Trainer")
    _mil(store, "x", 4, "TEX2")  # type alone is enough
    lake.write("positions", _fleet(4, NOW - timedelta(minutes=10), "t", cc)
               + _fleet(4, NOW - timedelta(minutes=10), "x", cc) + history([cc]))
    assert det.run_once()["created"] == {}


def test_concentration_fighter_surge_away_from_bases(env):
    lake, store, det, _ = env
    sites(det, bases=[{"lat": BASE[0], "lon": BASE[1] + 4, "type": "airbase"}])  # ~ 370 km
    _mil(store, "f", 8, "F16")
    lake.write("positions", _fleet(8, NOW - timedelta(minutes=10), "f")
               + history([BASE], n=2))  # baseline mean 2 -> 8 is 4x
    assert det.run_once()["created"] == {"concentration": 1}


def test_concentration_near_military_airfield_needs_3x(env):
    lake, store, det, _ = env
    sites(det, bases=[{"lat": BASE[0], "lon": BASE[1], "type": "airbase"}])
    _mil(store, "f", 8, "F16")
    lake.write("positions", _fleet(8, NOW - timedelta(minutes=10), "f")
               + history([BASE], n=3))  # baseline mean 3: 8 is 2.7x -> suppressed
    assert det.run_once()["created"] == {}
    sites(det, bases=[{"lat": BASE[0], "lon": BASE[1], "type": "airbase"}])
    det._baseline_at = None
    _mil(store, "g", 9, "F16")
    lake.write("positions", _fleet(9, NOW - timedelta(minutes=5), "g"))  # 17 now vs 3 -> 5.7x
    assert det.run_once()["created"] == {"concentration": 1}


def test_concentration_needs_three_days_of_baseline(env):
    lake, store, det, _ = env
    _mil(store, "f", 8, "F16")
    lake.write("positions", _fleet(8, NOW - timedelta(minutes=10), "f") + history([BASE], days=2))
    assert det.run_once()["created"] == {}


def test_concentration_ignores_light_civil_types(env):
    lake, store, det, _ = env
    _mil(store, "c", 9, "P28A")  # flying club Pipers on a base flagged military
    lake.write("positions", _fleet(9, NOW - timedelta(minutes=10), "c") + history([BASE]))
    assert det.run_once()["created"] == {}


def test_baseline_counts_only_buckets_with_collected_positions(env):
    lake, _store, det, _ = env
    rows = history([BASE], days=3, n=3)  # 3 aircraft at 11:xx on days 1-3
    for d in (4, 5):  # area has data on days 4-5, but collection was down at 11:xx
        rows.append(apos(f"air:z{d}", BASE, NOW.replace(hour=5) - timedelta(days=d)))
    lake.write("positions", rows)
    start = NOW - timedelta(minutes=30)
    det._refresh_baseline(NOW, start)
    import h3

    disk = set(h3.grid_disk(h3.latlng_to_cell(*BASE, 3), 1))
    mean, std, days = det._baseline_stats(disk, 11)
    assert (mean, std, days) == (3.0, 0.0, 3)  # was 1.8 over 5 "days" (outage = zeros)


def test_stale_detector_versions_are_purged_on_start(env):
    from osint.pipeline.sensor_events import DETECTOR_VERSIONS

    _lake, store, det, _ = env
    t = "2026-11-21T10:00:00.000+00:00"
    for eid, detector, details in (("C1", "concentration", {}),
                                   ("C2", "concentration", {"detector_version": 2}),
                                   ("O1", "orbit", {})):
        store.save_sensor_event(
            {"id": eid, "category": "air_activity", "title": eid, "t_start": t, "t_end": t,
             "status": "sensor-corroborated", "confidence": 0.6, "version": 1,
             "mention_count": 1, "source_diversity": 0, "first_seen": t, "updated_at": t},
            {"detector": detector, "dedupe_key": f"k:{eid}", "cell": "c", "details": details},
            [], {"event_id": eid, "score": 0.6, "kinds": [], "evidence": 0,
                 "corroborated": True})
    assert DETECTOR_VERSIONS["concentration"] == 2
    assert det.purge_stale() == 1
    assert {e["id"] for e in store.list_events(limit=10)} == {"C2", "O1"}
    assert store.sensor_event_ids() == {"C2", "O1"}
