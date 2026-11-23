import math
import random
from datetime import UTC, datetime, timedelta

import h3
import pytest

from osint.config import ROOT
from osint.core.geo import haversine_km
from osint.core.metrics import Metrics
from osint.core.models import Observation, Position, TrackInfo
from osint.pipeline.cluster import EventState
from osint.pipeline.corroborate import (
    CorroborationConfig,
    Corroborator,
    noisy_or,
    rollup,
    search_cells,
)
from osint.pipeline.events import EventsConfig, make_scorer
from osint.store.lake import Lake, observation_row, position_row
from osint.store.sqlite import SqliteStore

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
DEPOT = (48.00, 37.80)


def offset(lat, lon, km_north=0.0, km_east=0.0):
    return (lat + km_north / 111.2,
            lon + km_east / (111.2 * math.cos(math.radians(lat))))


def event(eid="E1", category="armed_conflict", title="Missile strike hits ammunition depot",
          where=DEPOT, precision="city", t0=NOW - timedelta(hours=2), t1=NOW - timedelta(hours=1)):
    lat, lon = where
    c = [h3.str_to_int(h3.latlng_to_cell(lat, lon, r)) for r in (3, 5, 7)]
    return EventState(id=eid, category=category, t_start=t0, t_end=t1, first_seen=t0,
                      updated_at=NOW, title=title, lat=lat, lon=lon, precision=precision,
                      geo_confidence=0.9, h3_3=c[0], h3_5=c[1], h3_7=c[2], domains={"a.x"},
                      relevance=0.8, has_article=True)


def fire(i, where, ts, frp=40.0, conf="high"):
    return observation_row(Observation(id=f"fire:{i}", type="fire", ts=ts, lat=where[0],
                                       lon=where[1], source="firms:viirs", value=frp,
                                       attrs={"confidence": conf}))


def quake(i, where, ts, mag=2.0, etype="explosion", depth=1.0):
    return observation_row(Observation(id=f"quake:{i}", type="quake", ts=ts, lat=where[0],
                                       lon=where[1], source="usgs", value=mag,
                                       attrs={"event_type": etype, "depth_km": depth}))


def pos(track, where, ts, kind="air", speed=None):
    return position_row(Position(track_id=track, kind=kind, ts=ts, lat=where[0], lon=where[1],
                                 source="t", speed_kts=speed))


@pytest.fixture
def env(tmp_path):
    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    m = Metrics()
    corr = Corroborator(store, lake, m, CorroborationConfig.from_file(
        ROOT / "config" / "corroboration.yaml"))
    yield lake, store, corr, m
    store.close()
    lake.close()


def run(corr, events, now=NOW):
    ecfg = EventsConfig.from_file(ROOT / "config" / "events.yaml")
    scorer = make_scorer(ecfg, lambda eid: corr.scores.get(eid, 0.0))
    return corr.run({e.id: e for e in events}, now, scorer)


def rows(store, eid="E1"):
    return store.list_corroborations(eid)


# ------------------------------------------------------------------------ H3


@pytest.mark.parametrize("lat,lon,radius", [
    (48.0, 37.8, 25), (0.1, 179.95, 50), (64.0, -20.0, 100), (-33.9, 18.4, 150),
    (26.5, 56.3, 300), (12.0, 45.0, 10)])
def test_search_cells_cover_the_circle_incl_neighbours(lat, lon, radius):
    rng = random.Random(1)
    res, cells = search_cells(lat, lon, radius)
    assert res in (3, 5) and len(cells) > 1  # always more than the centre cell
    for _ in range(400):
        bearing, d = rng.uniform(0, 2 * math.pi), radius * math.sqrt(rng.random())
        p = offset(lat, lon, d * math.cos(bearing), d * math.sin(bearing))
        if not -180 <= p[1] <= 180:
            p = (p[0], (p[1] + 540) % 360 - 180)
        if haversine_km(lat, lon, *p) > radius:
            continue
        assert h3.str_to_int(h3.latlng_to_cell(p[0], p[1], res)) in cells


def test_small_radius_uses_res5_large_uses_res3():
    assert search_cells(48.0, 37.8, 25)[0] == 5
    assert search_cells(48.0, 37.8, 150)[0] == 3


def test_noisy_or_rollup():
    assert noisy_or([0.5, 0.5]) == 0.75
    score, kinds = rollup([{"kind": "fire", "score": 0.5}, {"kind": "quake", "score": 0.5},
                           {"kind": "fire", "score": 0.0}], top=5)
    assert score == 0.75 and kinds == ["fire", "quake"]


# ---------------------------------------------------------------------- rules


def test_fire_rule_positive_and_negatives(env):
    lake, store, corr, m = env
    lake.write("observations", [
        fire(1, offset(*DEPOT, km_east=5), NOW - timedelta(hours=1, minutes=30)),  # in
        fire(2, offset(*DEPOT, km_north=40), NOW - timedelta(hours=1)),  # too far (25 km)
        fire(3, DEPOT, NOW + timedelta(hours=12)),  # after t_end + 12 h
        fire(4, DEPOT, NOW - timedelta(hours=9)),  # before t_start - 6 h
    ])
    res = run(corr, [event()])
    [r] = rows(store)
    assert (r["kind"], r["ref_id"], r["rule"]) == ("fire", "fire:1", "firms_hotspot")
    assert 4.5 < r["distance_km"] < 5.5 and r["dt_minutes"] == 0.0
    assert 0 < r["score"] <= 1 and r["details"]["frp_mw"] == 40.0
    assert res.new_rows == {"fire": 1} and m.get("corroborations_total", kind="fire") == 1


def test_fire_rule_needs_explosion_type_title(env):
    lake, store, corr, _ = env
    lake.write("observations", [fire(1, DEPOT, NOW - timedelta(hours=1))])
    run(corr, [event(title="Leaders discuss ceasefire terms")])
    assert rows(store) == []


def test_seismic_rule_explosion_and_shallow_vs_deep(env):
    lake, store, corr, _ = env
    lake.write("observations", [
        quake(1, offset(*DEPOT, km_north=10), NOW - timedelta(hours=1)),
        quake(2, offset(*DEPOT, km_north=20), NOW - timedelta(hours=1), etype="earthquake",
              mag=2.5, depth=2.0),
        quake(3, offset(*DEPOT, km_north=20), NOW - timedelta(hours=1), etype="earthquake",
              mag=2.5, depth=30.0),  # deep: not explosion-like
        quake(4, offset(*DEPOT, km_north=80), NOW - timedelta(hours=1)),  # beyond 50 km
    ])
    run(corr, [event()])
    got = {r["ref_id"]: r["rule"] for r in rows(store)}
    assert got == {"quake:1": "seismic_explosion", "quake:2": "shallow_quake"}


def test_disaster_quake_by_magnitude(env):
    lake, store, corr, _ = env
    tokyo = (35.68, 139.69)
    lake.write("observations", [
        quake(1, offset(*tokyo, km_east=150), NOW - timedelta(hours=3), mag=6.1,
              etype="earthquake", depth=20),
        quake(2, offset(*tokyo, km_east=50), NOW - timedelta(hours=3), mag=3.0,
              etype="earthquake", depth=20),
    ])
    run(corr, [event(category="disaster", title="Strong earthquake shakes Tokyo",
                     where=tokyo)])
    [r] = rows(store)
    assert r["ref_id"] == "quake:1" and r["rule"] == "quake_magnitude"


def test_disaster_hazard_alert_evidence(env):
    lake, store, corr, _ = env
    manila = (14.6, 121.0)

    def alert(i, where, level, source="gdacs"):
        return observation_row(Observation(
            id=f"hazard:{source}:{i}", type="hazard", ts=NOW - timedelta(hours=5),
            lat=where[0], lon=where[1], source=source, value=None,
            attrs={"source": source, "hazard": "tropical_cyclone", "alert_level": level,
                   "name": "Tropical Cyclone FUNG-26"}))

    lake.write("observations", [alert("TC:1", offset(*manila, km_east=100), "red"),
                                alert("EONET_1", offset(*manila, km_east=600), None,
                                      "eonet")])  # out of range
    res = run(corr, [event(category="disaster", title="Typhoon batters Manila",
                           where=manila)])
    [r] = rows(store)
    assert r["kind"] == "hazard" and r["ref_id"] == "hazard:gdacs:TC:1"
    assert r["rule"] == "gdacs_red" and r["details"]["alert_level"] == "red"
    assert r["score"] > 0.5 and "E1" in res.newly_corroborated
    # hazard alerts are not evidence for other categories
    run(corr, [event(eid="E2", title="Missile strike hits port", where=manila)])
    assert rows(store, "E2") == []


def test_aircraft_rule_military_only_in_window(env):
    lake, store, corr, _ = env
    store.upsert_tracks([
        TrackInfo(track_id="air:ae01", kind="air", ident="ae01", type_code="R135",
                  name="JAKE11", is_mil=True, high_priority=True),
        TrackInfo(track_id="air:ae02", kind="air", ident="ae02", type_code="C17", is_mil=True),
        TrackInfo(track_id="air:4ca1", kind="air", ident="4ca1", type_code="B738")],
        NOW)
    t = NOW - timedelta(hours=1, minutes=30)
    lake.write("positions", [
        *(pos("air:ae01", offset(*DEPOT, km_east=60 + i), t + timedelta(minutes=i))
          for i in range(5)),
        pos("air:ae02", offset(*DEPOT, km_north=120), NOW - timedelta(hours=8)),  # too early
        pos("air:4ca1", DEPOT, t),  # civil
    ])
    run(corr, [event(category="air_activity", title="Spy plane spotted near the border")])
    [r] = rows(store)
    assert r["kind"] == "aircraft" and r["ref_id"] == "air:ae01"
    assert r["details"]["type"] == "R135" and r["details"]["high_priority"]
    assert r["details"]["positions"] == 5 and 59 < r["distance_km"] < 61


def test_ship_loiter_and_ais_gap(env):
    lake, store, corr, _ = env
    hormuz = (26.5, 56.3)
    store.upsert_tracks([TrackInfo(track_id="sea:1", kind="sea", ident="1"),
                         TrackInfo(track_id="sea:2", kind="sea", ident="2"),
                         TrackInfo(track_id="sea:3", kind="sea", ident="3")], NOW)
    t0 = NOW - timedelta(hours=4)
    ps = [pos("sea:1", offset(*hormuz, km_east=10, km_north=0.1 * i), t0 + timedelta(
        minutes=20 * i), kind="sea", speed=0.5) for i in range(10)]  # loiters 3 h
    # sea:2 reports, goes dark 3 h across the event window, reappears
    ps += [pos("sea:2", offset(*hormuz, km_north=20), NOW - timedelta(hours=5), kind="sea",
               speed=10), pos("sea:2", offset(*hormuz, km_north=30),
                              NOW - timedelta(hours=2), kind="sea", speed=10)]
    # sea:3 reports every 30 min: no gap; it is just passing through (presence)
    ps += [pos("sea:3", offset(*hormuz, km_east=-30, km_north=i), NOW - timedelta(
        hours=4) + timedelta(minutes=30 * i), kind="sea", speed=12) for i in range(8)]
    lake.write("positions", ps)
    run(corr, [event(category="maritime_security", title="Tanker seized in the Strait",
                     where=hormuz, t0=NOW - timedelta(hours=3, minutes=30),
                     t1=NOW - timedelta(hours=3))])
    got = {(r["kind"], r["ref_id"].split("@")[0]): r for r in rows(store)}
    assert got[("ship", "sea:1")]["rule"] == "ship_loiter"
    assert got[("ship", "sea:3")]["rule"] == "ship_presence"
    gap = got[("ais_gap", "sea:2")]
    assert gap["rule"] == "ais_dark_gap" and gap["details"]["gap_h"] == 3.0
    assert ("ais_gap", "sea:1") not in got and ("ais_gap", "sea:3") not in got


def test_country_precision_is_skipped_admin1_discounted(env):
    lake, store, corr, m = env
    lake.write("observations", [fire(1, DEPOT, NOW - timedelta(hours=1))])
    res = run(corr, [event("EC", precision="country"), event("EA", precision="admin1"),
                     event("EP", precision="city")])
    assert res.skipped == {"precision": 1} and rows(store, "EC") == []
    a, p = rows(store, "EA")[0]["score"], rows(store, "EP")[0]["score"]
    assert a == pytest.approx(p * 0.5, abs=1e-3)
    assert m.get("corroboration_skipped_total", reason="precision") == 1


# ------------------------------------------------------------ status/incremental


def test_status_transition_idempotency_and_incremental(env):
    lake, store, corr, m = env
    ev = event()
    store.save_events([ev.to_row()])
    lake.write("observations", [
        fire(i, offset(*DEPOT, km_east=i), NOW - timedelta(hours=1, minutes=10 * i))
        for i in range(1, 4)])
    res = run(corr, [ev])
    assert ev.sensor_corroborated and ev.status == "sensor-corroborated"
    assert res.newly_corroborated == ["E1"] and m.get("events_sensor_corroborated_total") == 1
    saved = store.get_event("E1")
    assert saved["status"] == "sensor-corroborated" and saved["confidence"] == ev.confidence
    summ = store.corroboration_summaries(["E1"])["E1"]
    assert summ["corroborated"] and summ["kinds"] == ["fire"] and summ["score"] >= 0.5
    n = len(rows(store))
    # rerun: nothing new, event unchanged -> not even re-evaluated, no duplicates
    res2 = run(corr, [ev], NOW + timedelta(minutes=6))
    assert res2.evaluated == 0 and len(rows(store)) == n and not res2.new_rows
    # forced re-evaluation of the same data is also idempotent
    corr._evaluated.clear()
    res3 = run(corr, [ev], NOW + timedelta(minutes=12))
    assert res3.evaluated == 1 and not res3.new_rows and len(rows(store)) == n
    assert not res3.newly_corroborated
    # a new observation in the event's area makes it dirty again
    lake.write("observations", [fire(9, DEPOT, NOW - timedelta(hours=1))])
    res4 = run(corr, [ev], NOW + timedelta(minutes=18))
    assert res4.evaluated == 1 and res4.new_rows == {"fire": 1}
    # ... but one far away does not
    lake.write("observations", [fire(10, (10.0, 10.0), NOW - timedelta(hours=1))])
    assert run(corr, [ev], NOW + timedelta(minutes=24)).evaluated == 0


def test_evidence_that_no_longer_matches_is_removed(env):
    lake, store, corr, _ = env
    ev = event()
    lake.write("observations", [fire(1, DEPOT, NOW - timedelta(hours=1))])
    run(corr, [ev])
    assert len(rows(store)) == 1
    store.save_corroborations([{"event_id": "E1", "kind": "imagery", "ref_id": "s2:x",
                                "rule": "change", "score": 0.3}])
    ev.lat, ev.lon = 10.0, 10.0  # relocated: the fire is now far away
    ev.updated_at = NOW + timedelta(minutes=1)
    run(corr, [ev], NOW + timedelta(minutes=6))
    assert [r["kind"] for r in rows(store)] == ["imagery"]  # other owners' rows kept
    assert store.corroboration_summaries(["E1"])["E1"]["kinds"] == ["imagery"]


def test_event_builder_runs_corroboration(tmp_path):
    from osint.core.models import Document, PlaceHit, TriageResult
    from osint.pipeline.events import EventBuilder
    from osint.store.lake import document_row

    lake = Lake(tmp_path / "lake")
    store = SqliteStore(tmp_path / "s.sqlite")
    m = Metrics()
    corr = Corroborator(store, lake, m, CorroborationConfig.from_file(
        ROOT / "config" / "corroboration.yaml"))
    b = EventBuilder(store, lake, m, EventsConfig.from_file(ROOT / "config" / "events.yaml"),
                     corroborator=corr, clock=lambda: NOW)
    hit = PlaceHit(name="Druzhkivka", country="UA", lat=DEPOT[0], lon=DEPOT[1],
                   precision="city", confidence=0.9, matched="Druzhkivka")
    d = Document(id="doc:1", source="rss:t", url="https://a.example/1",
                 title="Missile strike hits ammunition depot in Druzhkivka", snippet="",
                 language="en", published_at=NOW - timedelta(hours=1),
                 fetched_at=NOW - timedelta(minutes=50), kind="article")
    lake.write("documents", [document_row(d, TriageResult(
        language="en", relevance=0.8, categories=["armed_conflict"], places=[hit],
        best_place=hit))])
    lake.write("observations", [
        fire(i, offset(*DEPOT, km_east=i), NOW - timedelta(minutes=50)) for i in range(1, 4)])
    s = b.run_once()
    assert s.created == 1 and s.corroboration_evaluated == 1 and s.sensor_corroborated == 1
    [ev] = store.list_events(limit=5)
    assert ev["status"] == "sensor-corroborated"
    # a restarted builder keeps the sensor term in confidence (scores warm-started)
    b2 = EventBuilder(store, lake, m, EventsConfig.from_file(ROOT / "config" / "events.yaml"),
                      corroborator=Corroborator(store, lake, m, corr.cfg), clock=lambda: NOW)
    b2.warm_start()
    assert b2.sensor_scores[ev["id"]] >= 0.5
    store.close()
    lake.close()
