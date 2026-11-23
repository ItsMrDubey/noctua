from pathlib import Path

import pytest

from osint.config import ROOT
from osint.fires.config import FireScoreConfig
from osint.fires.geo import PointIndex, SegmentIndex
from osint.fires.score import Context, Hotspot, Pool, Recurrence, score_hotspots, score_one

CFG = FireScoreConfig()
FRONT = SegmentIndex([[(48.0, 37.0), (48.5, 37.5)]])  # a short NE-running front line


def hs(i, lat, lon, frp=10.0, dn="N", conf="Nominal"):
    return Hotspot(id=f"fire:{i}", ts_us=0, lat=lat, lon=lon, frp=frp, daynight=dn,
                   confidence=conf)


def one(h, ctx, pool=None):
    return score_one(h, Pool(pool or [h]), ctx, CFG)


def test_repo_config_matches_defaults():
    loaded = FireScoreConfig.load(ROOT / "config" / "fire_score.yaml")
    assert loaded.weights == CFG.weights
    assert loaded.fingerprint() == CFG.fingerprint()
    assert loaded.job.threshold == 50


def test_config_overrides_and_fingerprint():
    cfg = FireScoreConfig.from_dict({"weights": {"night": 20}, "pattern": {"high_frp": 99},
                                     "job": {"interval_s": 1, "bogus": 1}})
    assert cfg.w("night") == 20 and cfg.pattern.high_frp == 99 and cfg.job.interval_s == 1
    assert cfg.fingerprint() != CFG.fingerprint()
    # cadence does not change what a score means
    assert FireScoreConfig.from_dict({"job": {"interval_s": 1}}).fingerprint() == \
        CFG.fingerprint()
    assert FireScoreConfig.load(Path("/nonexistent.yaml")).weights == CFG.weights


def test_isolated_night_fire_on_the_front_line_is_war_likely():
    r = one(hs(1, 48.25, 37.25, frp=60), Context(frontline=FRONT))
    assert r["score"] >= 70
    assert r["reasons"][0] == "near_frontline"
    assert {"night", "isolated", "high_frp"} <= set(r["reasons"])
    assert r["features"]["frontline_km"] == 0.0


def test_front_line_points_decay_with_distance():
    near = one(hs(1, 48.25, 37.25), Context(frontline=FRONT))["score"]
    mid = one(hs(2, 48.25, 37.45), Context(frontline=FRONT))  # ~10.5 km
    far = one(hs(3, 48.25, 37.85), Context(frontline=FRONT))  # ~31 km
    beyond = one(hs(4, 48.25, 38.5), Context(frontline=FRONT))
    assert near > mid["score"] > far["score"] > beyond["score"]
    assert "near_frontline" in mid["reasons"] and "frontline_zone" in far["reasons"]
    assert "frontline_km" not in beyond["features"]


def test_pattern_alone_stays_low_far_from_conflict():
    ctx = Context(cities=PointIndex([(39.1, -94.6, ("Kansas City", 10.0))]))
    r = one(hs(1, 39.1, -94.6, frp=80), ctx)
    assert {"urban", "night", "isolated", "high_frp"} <= set(r["reasons"])
    assert r["score"] < 20


def test_large_cluster_and_burn_region_are_demoted():
    pool = [hs(i, 10 + (i % 20) * 0.004, 20 + (i // 20) * 0.004) for i in range(400)]
    ctx = Context(frontline=SegmentIndex([[(10.0, 20.0), (10.5, 20.0)]]))
    rows = score_hotspots(pool[:1], Pool(pool), ctx, CFG)
    r = rows[0]
    assert {"large_cluster", "burn_region"} <= set(r["reasons"])
    assert "isolated" not in r["reasons"]
    lone = one(hs(9, 10.2, 20.0), ctx)
    assert r["score"] < lone["score"] - 30


def test_duplicate_detections_of_one_site_stay_isolated():
    # one site, four sensors on two overpasses: still a single ~450 m site
    pool = [hs(i, 48.25 + (i % 2) * 0.0005, 37.25) for i in range(8)]
    r = score_hotspots(pool[:1], Pool(pool), Context(), CFG)[0]
    assert "isolated" in r["reasons"] and r["features"]["sites_2km"] == 0


def test_conflict_news_tiers():
    news = PointIndex([(48.0, 30.0, ("ev1", "Strike on depot", "point")),
                       (49.0, 30.0, ("ev2", "Shelling of town", "city"))])
    ctx = Context(news=news)
    r = one(hs(1, 48.02, 30.0), ctx)
    assert r["reasons"][0] == "conflict_news_5km"
    assert r["features"]["news_event"] == {"id": "ev1", "title": "Strike on depot"}
    r = one(hs(2, 49.08, 30.0), ctx)  # ~9 km from a city-precision event
    assert "conflict_news_city" in r["reasons"] and r["features"]["news_event"]["id"] == "ev2"
    r = one(hs(3, 48.15, 30.0), ctx)  # ~17 km from a point event
    assert "conflict_news_25km" in r["reasons"]


def test_ucdp_density_log_scale():
    pts = [(15.0 + i * 0.001, 30.0, None) for i in range(25)]
    ctx = Context(ucdp=PointIndex(pts))
    r = one(hs(1, 15.0, 30.0), ctx)
    assert "ucdp_hist" in r["reasons"] and r["features"]["ucdp_n"] == 25
    assert r["features"]["points"]["ucdp_hist"] == pytest.approx(25.0)
    few = one(hs(2, 15.0, 30.0), Context(ucdp=PointIndex(pts[:2])))
    assert "ucdp_weak" in few["reasons"] and "ucdp_hist" not in few["reasons"]


def test_static_sources_are_demoted():
    ctx = Context(frontline=FRONT, volcanoes=PointIndex([(48.25, 37.25, "Test volcano")]))
    r = one(hs(1, 48.25, 37.25), ctx)
    assert "volcano" in r["reasons"] and r["features"]["volcano"] == "Test volcano"
    plain = one(hs(1, 48.25, 37.25), Context(frontline=FRONT))
    assert r["score"] <= plain["score"] - 50

    flare = one(hs(2, 30.5, 47.75, dn="D"),
                Context(flares=PointIndex([(30.5, 47.751, "upstream")])))
    assert flare["reasons"][0] == "gas_flare" and flare["score"] == 0


def recurrence(lat, lon, days_mask, median=2.0, history=10):
    rec = Recurrence({}, 0.004, history)
    rec.cells[rec.key(lat, lon)] = (days_mask, median)
    return rec


def test_recurring_cell_is_a_static_site_unless_far_above_its_usual_frp():
    rec = recurrence(52.55, 39.6, 0b1111111)  # seen on 7 of 10 past days
    r = one(hs(1, 52.5501, 39.6001, frp=5), Context(recurrence=rec))
    assert "static_site" in r["reasons"] and r["features"]["static_days"] == 7
    hot = one(hs(2, 52.5501, 39.6001, frp=150), Context(recurrence=rec))
    assert "static_anomaly" in hot["reasons"] and "static_site" not in hot["reasons"]
    assert hot["features"]["static_overridden"] == ["static_site"]
    # a neighbouring cell (geolocation jitter) counts too
    assert "static_site" in one(hs(3, 52.5541, 39.6, frp=5), Context(recurrence=rec))["reasons"]


def test_recurrence_needs_enough_history():
    rec = recurrence(52.55, 39.6, 0b111, history=3)
    r = one(hs(1, 52.5501, 39.6001), Context(recurrence=rec))
    assert "static_site" not in r["reasons"] and "static_days" not in r["features"]
    rec = recurrence(52.55, 39.6, 0b11, history=10)  # 2 of 10 days: below min_days
    assert "static_site" not in one(hs(1, 52.5501, 39.6001), Context(recurrence=rec))["reasons"]


def test_low_confidence_and_infrastructure():
    ctx = Context(frontline=FRONT, pois=PointIndex([(48.25, 37.26, ("Test AB", "airbase"))]))
    r = one(hs(1, 48.25, 37.25, conf="Low (12%)", dn="D"), ctx)
    assert "low_conf" in r["reasons"] and "infrastructure" in r["reasons"]
    assert r["features"]["facility"] == "Test AB (airbase)"
    assert "night" not in r["reasons"]


def test_context_is_capped_and_score_clamped():
    pts = [(48.25, 37.25, None)] * 30
    news = PointIndex([(48.25, 37.25, ("e", "t", "point"))])
    ctx = Context(frontline=FRONT, ucdp=PointIndex(pts), news=news)
    r = one(hs(1, 48.25, 37.25, frp=90), ctx)
    assert r["features"]["context"] == CFG.w("context_cap")
    assert r["score"] == 100
    assert 0 <= one(hs(2, 0, 0, conf="low", dn="D"),
                    Context(volcanoes=PointIndex([(0, 0, "v")])))["score"] == 0
