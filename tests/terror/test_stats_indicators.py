import math
from datetime import UTC, date, datetime

import pytest

from osint.terror import evaluate, indicators, stats


def test_poisson_tails_match_closed_form():
    t = stats.poisson_test(0, 2.0)
    assert t["p_low"] == pytest.approx(math.exp(-2), rel=1e-9) and t["p_high"] == 1.0
    t = stats.poisson_test(5, 1.0)
    p_ge5 = 1 - sum(math.exp(-1) / math.factorial(i) for i in range(5))
    assert t["p_high"] == pytest.approx(p_ge5, rel=1e-9)


def test_negative_binomial_is_wider_than_poisson_for_overdispersed_baselines():
    pois = stats.poisson_test(30, 15.0)
    nb = stats.nb_test(30, 15.0, 120.0)
    assert nb["model"] == "negative_binomial" and nb["p_high"] > pois["p_high"]
    assert stats.nb_test(30, 15.0, 15.0)["model"] == "poisson"  # not over-dispersed


def test_chi_square_survival_known_values():
    assert stats._chi2_sf(3.841, 1) == pytest.approx(0.05, abs=1e-3)
    assert stats._chi2_sf(9.488, 4) == pytest.approx(0.05, abs=1e-3)
    assert stats._chi2_sf(0.0, 3) == 1.0


def test_chi2_shift_needs_data_and_detects_shift():
    assert stats.chi2_shift({"a": 2}, {"a": 50}) is None
    res = stats.chi2_shift({"bombing_ied": 2, "stabbing": 18}, {"bombing_ied": 80, "stabbing": 20})
    assert res["p"] < 0.001


def test_rate_ci_brackets_the_estimate():
    lo, hi = stats.rate_ci(10, 2.0)
    assert lo < 5 < hi and lo == pytest.approx(2.70, abs=0.05)


def test_wilson_interval():
    assert evaluate.wilson(9, 10) == [0.596, 0.982]
    assert evaluate.wilson(0, 0) is None


def test_month_range_and_upcoming_dates():
    assert indicators.month_range("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01",
                                                            "2026-02"]
    cal = [{"month_day": "12-24", "label": "Christmas"}, {"month_day": "09-11", "label": "x"}]
    up = indicators.upcoming_dates(cal, date(2026, 11, 23))
    assert [c["label"] for c in up] == ["Christmas"] and up[0]["in_days"] == 31


def test_tempo_zero_fills_months_without_events():
    rows = [{"month": "2026-09", "events": 6, "deaths_best": 6, "group_id": "jnim",
             "category": "jihadist"}]
    t = indicators.ucdp_tempo(rows, ("2025-01", "2026-10"))
    assert t["baseline_monthly_mean"] == 0 and t["observed"] == 6
    assert t["verdict"] == "above baseline"


def test_criminal_groups_excluded_from_terror_base_rate():
    rows = [{"month": "2025-03", "events": 50, "deaths_best": 90, "group_id": "sinaloa",
             "category": "criminal"},
            {"month": "2025-03", "events": 5, "deaths_best": 9, "group_id": "isis",
             "category": "jihadist"}]
    r = indicators.base_rates(rows, ("2021-01", "2025-12"))
    assert r["years"][-1] == {"year": "2025", "events": 5, "deaths": 9}


def test_posture_components_and_formula_are_exposed():
    pz = indicators.posture(levels=[{"authority": "SGDSN", "level": "vigilance renforcée",
                                     "rank": 2, "scale_max": 3, "scope": "national"}],
                            advisory=None, rates={"available": False}, tempo={"available": False},
                            recent_incidents=0)
    assert pz["available"] and set(pz["components"]) == {"official_level", "recent_incidents"}
    assert "formula" in pz and pz["band"][0] <= pz["index"] <= pz["band"][1]


def test_ntas_is_not_used_as_a_level_component():
    pz = indicators.posture(levels=[{"authority": "DHS National Terrorism Advisory System",
                                     "level": "No current advisory", "rank": 1, "scale_max": 4,
                                     "scope": "national"}], advisory=None,
                            rates={"available": False}, tempo={"available": False},
                            recent_incidents=None)
    assert pz["available"] is False


def test_news_tempo_counts_unique_live_incidents_by_event_time():
    now = datetime(2026, 11, 23, tzinfo=UTC)
    mk = lambda s, st="reported", f=False, m=None: {"klass": "attack", "status": st,
                                                    "sort_time": s, "follow_up": f}
    incs = [mk("2026-11-20T00:00:00+00:00"), mk("2026-11-21T00:00:00+00:00", "confirmed"),
            mk("2026-11-22T00:00:00+00:00", "possible"), mk("2026-11-01T00:00:00+00:00", f=True),
            mk("2026-06-01T00:00:00+00:00")]
    t = indicators.news_tempo(incs, now, coverage_days=200)
    assert t["observed"] == 2 and t["confirmed"] == 1 and t["available"] is True
