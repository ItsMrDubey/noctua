"""Pure metric functions on hand-built inputs (edge cases)."""

from collections import Counter
from datetime import UTC, datetime, timedelta

from osint.benchmark.config import BenchmarkConfig
from osint.benchmark.sample import allocate
from osint.benchmark.scorecard import (
    EventRow,
    Inputs,
    label_summary,
    latency,
    quantile,
    residual_duplicates,
    score,
)
from osint.benchmark.store import flatten

NOW = datetime(2026, 11, 21, 12, tzinfo=UTC)
CFG = BenchmarkConfig()


def ev(i, **kw):
    base = {"id": f"e{i}", "category": "armed_conflict", "precision": "city",
            "status": "reported", "source_diversity": 1, "first_seen": NOW,
            "t_start": NOW}
    base.update(kw)
    return EventRow(**base)


def test_zero_events_scores_cleanly():
    card = score(Inputs(now=NOW, start=NOW - timedelta(hours=24)), CFG, "24h")
    m = card["metrics"]
    assert m["duplication"]["gdelt"]["rows_per_incident"] is None
    assert m["duplication"]["dedup_ratio"] is None
    assert m["duplication"]["statement"].startswith("No GDELT row")
    assert m["geolocation"]["ours"]["city_or_better_pct"] is None
    assert m["verification"]["ours"]["supported_pct"] is None
    assert m["noise_rejection"]["rejected_pct"] is None
    assert m["cost"]["ours"]["cost_per_verified_event_usd"] is None
    assert any("Small sample" in c for c in card["caveats"])
    assert all(h["ours"] is None or h["ours"] == 0 for h in card["headline"])


def test_no_gdelt_overlap_and_missing_verdicts():
    inp = Inputs(now=NOW, start=NOW - timedelta(hours=24),
                 events=[ev(1, articles=1, first_article=NOW - timedelta(minutes=5)),
                         ev(2, precision="unlocated", h3_5=None)])
    card = score(inp, CFG, "24h")
    m = card["metrics"]
    assert m["coverage"]["both"] == 0 and m["coverage"]["ours_only"] == 2
    assert m["latency"]["headline"]["pairs"] == 0
    assert m["latency"]["ours_time_to_event_live"]["median_min"] == 5.0
    assert m["verification"]["ours"]["verdicts"]["not_checked"] == 2
    assert m["verification"]["ours"]["supported_pct_of_checked"] is None


def test_latency_basis_all_and_cap():
    live_since = NOW - timedelta(hours=1)
    rows = [ev(1, first_seen=NOW, first_article=NOW - timedelta(hours=3),
               first_gdelt=NOW - timedelta(hours=2)),
            # absurd clock: dropped
            ev(2, first_seen=NOW, first_article=NOW - timedelta(days=30),
               first_gdelt=NOW)]
    out = latency(Inputs(now=NOW, start=NOW - timedelta(days=1), events=rows,
                         live_since=live_since), CFG)
    assert out["basis"] == "all" and out["live"]["pairs"] == 0
    assert out["all"]["pairs"] == 1 and out["headline"]["ours_median_min"] == 180.0
    assert out["headline"]["we_were_first_pct"] == 0.0
    card = score(Inputs(now=NOW, start=NOW - timedelta(days=1), events=rows,
                        live_since=live_since), CFG, "24h")
    assert any("cold-start" in c for c in card["caveats"])


def test_residual_duplicates_window_and_precision():
    t = NOW
    rows = [ev(1, h3_5="c", t_start=t), ev(2, h3_5="c", t_start=t + timedelta(hours=2)),
            ev(3, h3_5="c", t_start=t + timedelta(hours=40)),
            ev(4, h3_5="c", t_start=t, precision="country"),
            ev(5, h3_5="c", t_start=t, category="disaster")]
    assert residual_duplicates(rows, CFG) == 1


def test_sensor_derived_excluded_from_news_metrics():
    rows = [ev(1), ev(2, sensor_derived=True, precision="point")]
    card = score(Inputs(now=NOW, start=NOW - timedelta(hours=24), events=rows), CFG, "24h")
    assert card["metrics"]["geolocation"]["ours"]["events"] == 1
    assert card["metrics"]["coverage"]["sensor_derived_events"] == 1
    assert card["metrics"]["cost"]["ours"]["verified_events"] == 1


def test_noise_and_centroid_counts():
    inp = Inputs(now=NOW, start=NOW - timedelta(hours=24),
                 decisions=Counter({("rejected", "stale"): 8, ("rejected",
                                    "irrelevant_category"): 1, ("accepted",
                                    "duplicate_of_event"): 1}),
                 rejected_geo=Counter({("stale", "1"): 3, ("irrelevant_category", "4"): 1}))
    m = score(inp, CFG, "24h")["metrics"]
    assert m["noise_rejection"]["rejected_pct"] == 90.0
    assert m["noise_rejection"]["rejected_pct_excl_stale"] == 50.0
    assert m["geolocation"]["gdelt_country_centroid_rejected"]["total"] == 3


def test_label_summary_threshold():
    labels = {f"e{i}": {"correct_event": i % 4 != 0, "correct_location": i % 2 == 0}
              for i in range(19)}
    assert label_summary(labels, {}, 20)["status"] == "insufficient labels"
    labels["e19"] = {"correct_event": True, "correct_location": False}
    out = label_summary(labels, {"e0": "city", "e1": "country"}, 20)
    assert out["status"] == "ok" and out["labels"] == 20
    assert out["event_correctness_pct"] == 75.0
    assert out["precision_at_location_pct"] == 50.0
    assert out["by_precision"]["city"] == {"labels": 1, "correct": 1,
                                           "precision_at_location_pct": 100.0}


def test_helpers():
    assert quantile([], 0.5) is None
    assert quantile([1.0], 0.9) == 1.0
    assert quantile([0.0, 10.0], 0.9) == 9.0
    assert flatten({"a": {"b": 1, "c": None, "d": True, "e": [1]}, "f": 2.5}) == {
        "a.b": 1.0, "f": 2.5}
    assert allocate({"a": 90, "b": 9, "c": 1}, 10) == {"a": 7, "b": 2, "c": 1}
    assert allocate({"a": 2, "b": 1}, 50) == {"a": 2, "b": 1}
    assert allocate({}, 5) == {}
    assert sum(allocate({"a": 5, "b": 5, "c": 5}, 7).values()) == 7
