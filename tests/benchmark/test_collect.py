"""Scorecard over the synthetic stores: every metric against hand-computed answers."""

from datetime import timedelta

import pytest

from osint.benchmark.config import BenchmarkConfig
from osint.store.lake import Lake
from tests.benchmark.fixtures import NOW, build, service


@pytest.fixture
def env(tmp_path):
    built = build(tmp_path)
    yield tmp_path, built["lake"]
    built["lake"].close()


@pytest.fixture
def card(env):
    tmp, lake = env
    svc = service(tmp, lake)
    yield svc.compute("24h")
    svc.store.close()


def test_envelope(card):
    assert card["window"] == "24h" and card["as_of"].startswith("2026-11-21T12:00")
    assert set(card["metrics"]) == {"duplication", "geolocation", "latency", "coverage",
                                    "verification", "noise_rejection", "cost", "quality"}
    for m in card["metrics"].values():
        assert isinstance(m["definition"], str) and len(m["definition"]) > 40
    assert {h["metric"] for h in card["headline"]} >= {"duplication", "geolocation",
                                                       "latency", "cost"}
    assert card["caveats"]


def test_duplication(card):
    d = card["metrics"]["duplication"]
    assert d["gdelt"]["rows_attached"] == 4 and d["gdelt"]["incidents"] == 2
    assert d["gdelt"]["rows_per_incident"] == 2.0 and d["gdelt"]["max_rows_per_incident"] == 3
    assert d["ours"]["events"] == 5 and d["ours"]["possible_residual_duplicates"] == 1
    assert d["ours"]["events_per_incident_est"] == 1.25
    assert d["dedup_ratio"] == 1.6
    assert d["statement"] == "GDELT emitted 4 rows for 2 incidents we merged into 2 events"
    # tracked lake rows in window: g1 g2 g3 g4 g7 g8 g9 g10; g1-g3 share a key
    assert d["gdelt"]["window_rows_tracked"] == 8
    assert d["gdelt"]["window_distinct_keys_tracked"] == 6


def test_geolocation(card):
    g = card["metrics"]["geolocation"]
    assert g["ours"]["by_precision"] == {"point": 1, "city": 2, "admin1": 0, "country": 1,
                                         "unlocated": 1}
    assert g["ours"]["city_or_better_pct"] == 60.0
    assert g["ours"]["city_or_better_pct_of_located"] == 75.0
    t = g["gdelt"]["tracked"]
    assert t["rows"] == 8 and t["city_or_better_pct"] == 50.0
    assert t["country_level_pct"] == 37.5
    assert g["gdelt"]["all"]["rows"] == 10
    assert g["gdelt_country_centroid_rejected"] == {
        "total": 2, "by_reason": {"stale": 1, "no_article_backing": 1}}
    assert g["matched_rows"] == {"ours_finer": 1, "same": 3, "ours_coarser": 0}


def test_latency(card):
    lat = card["metrics"]["latency"]
    assert lat["basis"] == "live"
    h = lat["headline"]
    assert h["pairs"] == 2 and h["ours_median_min"] == 15.0
    assert h["gdelt_median_min"] == 12.5 and h["we_were_first_pct"] == 50.0
    assert lat["ours_time_to_event_live"] == {"events": 4, "median_min": 10.0,
                                              "p90_min": 17.0}


def test_coverage_and_noise(card):
    c = card["metrics"]["coverage"]
    assert c["both"] == 2 and c["ours_only"] == 3 and c["ours_only_pct"] == 60.0
    assert c["gdelt_only_relevant"] == 3 and c["gdelt_only_relevant_incidents_est"] == 3
    assert c["gdelt_rows_in_window"] == 10 and c["gdelt_rows_not_adjudicated"] == 2
    assert c["sensor_derived_events"] == 1
    n = card["metrics"]["noise_rejection"]
    assert n["adjudicated"] == 9 and n["rejected"] == 5 and n["rejected_pct"] == 55.56
    assert n["rejected_pct_excl_stale"] == 50.0
    assert n["by_reason"]["no_article_backing"] == {"rows": 2, "pct": 22.22}


def test_verification_and_cost(card):
    v = card["metrics"]["verification"]["ours"]
    assert v["verdicts"] == {"supported": 1, "mismatch": 1, "unsupported": 1,
                             "not_checked": 2}
    assert v["supported_pct"] == 20.0 and v["checked_pct"] == 60.0
    assert v["multi_source"] == 1 and v["sensor_corroborated"] == 1
    assert "by construction" in card["metrics"]["verification"]["gdelt"]["capability"]
    c = card["metrics"]["cost"]["ours"]
    assert c["llm_usd_window"] == 0.02 and c["llm_usd_all_time"] == 1.02
    assert c["verified_events"] == 3 and c["cost_per_verified_event_usd"] == 0.0067
    assert card["metrics"]["cost"]["notes"]  # AlterLab not metered
    assert card["metrics"]["quality"]["status"] == "insufficient labels"


def test_7d_window_includes_old_event(env):
    tmp, lake = env
    svc = service(tmp, lake)
    card = svc.compute("7d")
    assert card["metrics"]["duplication"]["ours"]["events"] == 6
    assert card["metrics"]["cost"]["ours"]["llm_usd_window"] == 1.02
    svc.store.close()


def test_no_lake_no_extraction(env):
    tmp, _ = env
    svc = service(tmp, None, extraction=False)
    card = svc.compute("24h")
    assert card["metrics"]["geolocation"]["gdelt"]["tracked"]["rows"] == 0
    assert card["metrics"]["geolocation"]["gdelt"]["tracked"]["city_or_better_pct"] is None
    assert card["metrics"]["cost"]["ours"]["spend_usd_window"] == 0.0
    assert card["metrics"]["duplication"]["gdelt"]["rows_attached"] == 4
    svc.store.close()


def test_empty_stores(tmp_path):
    lake = Lake(tmp_path / "lake")
    svc = service(tmp_path, lake)  # osint.sqlite does not exist
    card = svc.compute("24h")
    assert card["metrics"]["duplication"]["ours"]["events"] == 0
    assert card["metrics"]["latency"]["headline"]["pairs"] == 0
    assert card["metrics"]["cost"]["ours"]["cost_per_verified_event_usd"] is None
    assert card["live_since"] is None
    svc.store.close()
    lake.close()


def test_snapshot_history_and_cache(env):
    tmp, lake = env
    svc = service(tmp, lake, BenchmarkConfig(windows={"24h": 24.0}))
    out = svc.snapshot()
    assert out["24h"] > 20
    hist = svc.store.history("24h", NOW - timedelta(days=1),
                             "duplication.gdelt.rows_per_incident")
    assert hist == {"duplication.gdelt.rows_per_incident": [
        {"ts": "2026-11-21T12:00:00+00:00", "value": 2.0}]}
    assert svc.store.latest_scorecard("24h")["window"] == "24h"
    assert svc.store.prune(NOW + timedelta(hours=2)) > 0
    svc.store.close()


def test_historical_events_are_not_scored(tmp_path):
    from osint.benchmark.scorecard import _load_events
    from osint.store.sqlite import SqliteStore, connect, iso

    built = build(tmp_path)
    built["lake"].close()
    store = SqliteStore(tmp_path / "osint.sqlite")
    t = iso(NOW - timedelta(hours=1))
    store.save_events([{"id": "ucdp-1", "category": "armed_conflict", "title": "u",
                        "t_start": t, "t_end": t, "status": "historical", "confidence": 0.9,
                        "version": 1, "mention_count": 1, "source_diversity": 1,
                        "first_seen": t, "updated_at": t, "precision": "point",
                        "cluster_state": {"source": "ucdp"}}])
    store.close()
    db = connect(tmp_path / "osint.sqlite", read_only=True)
    ids = {e.id for e in _load_events(db, NOW - timedelta(hours=24))}
    db.close()
    assert len(ids) == 6 and "ucdp-1" not in ids
