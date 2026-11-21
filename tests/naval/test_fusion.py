from datetime import timedelta

from osint.naval import fusion as F
from osint.naval.models import Evidence
from tests.naval.helpers import NOW, cfg, synthetic_geo


def ev(kind, lat, lon, w, key, age_h=0.0, radius=0.0, names=()):
    return Evidence(kind=kind, ts=NOW - timedelta(hours=age_h), lat=lat, lon=lon, weight=w,
                    description=f"{kind} test", refs=[{"type": "track", "id": key}],
                    radius_km=radius, source_key=key, names=list(names))


def fuse(evs, usni=None):
    geo = synthetic_geo()
    fc = cfg().sec("fusion")
    s = F.build_surface(evs, NOW, fc, geo.mask)
    return s, F.hypotheses(evs, s, fc, usni=usni, usni_cfg=cfg().sec("usni"))


def test_surface_is_normalised_and_ocean_only():
    geo = synthetic_geo()
    s, _ = fuse([ev("sea_terminating_flight", 0.0, 3.0, 3.0, "a")])
    cells = s.cells_out()
    assert cells[0]["p"] == 1.0 and all(0 < c["p"] <= 1 for c in cells)
    assert not any(geo.mask.is_land_cell(c["h3"]) for c in cells)
    assert cells[0]["boundary"][0] == cells[0]["boundary"][-1]


def test_two_independent_kinds_make_a_confident_hypothesis():
    evs = [ev("sea_terminating_flight", 0.0, 9.0, 3.0, "air:c2"),
           ev("sea_terminating_flight", 0.3, 9.2, 2.5, "air:v22"),
           ev("support_ship", -0.2, 9.1, 2.5, "sea:pecos"),
           ev("news", 5.0, 5.0, 0.3, "news:x", radius=600, names=["USS Nimitz"])]
    _, hyps = fuse(evs)
    assert len(hyps) == 1
    h = hyps[0]
    assert h["confidence"] > 0.5
    assert h["kinds"] == ["sea_terminating_flight", "support_ship"]
    assert h["radius_km"] >= 50
    assert abs(h["lat"]) < 0.5 and abs(h["lon"] - 9.1) < 0.5
    assert {e["refs"][0]["id"] for e in h["evidence"]} >= {"air:c2", "air:v22", "sea:pecos"}
    assert h["candidate_names"][0]["name"] == "USS Nimitz"


def test_single_kind_is_capped_and_repeated_source_counts_once():
    many = [ev("sea_terminating_flight", 0.0, 9.0 + i * 0.05, 3.0, "air:same") for i in range(8)]
    _, hyps = fuse(many)
    assert len(hyps) == 1 and hyps[0]["confidence"] <= 0.45
    diverse = [ev("sea_terminating_flight", 0.0, 9.0 + i * 0.05, 3.0, f"air:{i}")
               for i in range(8)]
    _, hyps2 = fuse(diverse)
    assert hyps2[0]["confidence"] <= 0.45  # still a single kind
    fc = cfg().sec("fusion")
    s1 = F.kind_strengths(many, {i: 1.0 for i in range(8)}, [3.0] * 8, fc)
    s2 = F.kind_strengths(diverse, {i: 1.0 for i in range(8)}, [3.0] * 8, fc)
    assert s1["sea_terminating_flight"] < s2["sea_terminating_flight"]


def test_names_and_priors_alone_never_make_hypotheses():
    evs = [ev("news", 0.0, 9.0, 0.3, "n1", radius=600, names=["USS Nimitz"]),
           ev("usni_prior", 0.0, 9.0, 0.4, "u1", radius=700)]
    s, hyps = fuse(evs)
    assert hyps == [] and s.density


def test_separate_clusters_and_time_decay():
    evs = [ev("sea_terminating_flight", 0.0, 9.0, 3.0, "a"), ev("nav_warning", 0.1, 9.1, 2.5, "w"),
           ev("sea_terminating_flight", 0.0, -9.0, 3.0, "b"), ev("orbit", 0.0, -9.2, 1.5, "o")]
    _, hyps = fuse(evs)
    assert len(hyps) == 2
    old = [ev("sea_terminating_flight", 0.0, 9.0, 3.0, "a", age_h=96)]
    _, none = fuse(old)
    assert none == []
    assert F.decay(old[0], NOW, 12) < 3.0 / 200


def test_confidence_rule():
    fc = cfg().sec("fusion")
    c1, k1, _ = F.confidence({"sea_terminating_flight": 1.0, "news": 0.15}, fc)
    assert c1 <= 0.45 and k1 == ["sea_terminating_flight"]
    c2, k2, i2 = F.confidence({"sea_terminating_flight": 0.8, "nav_warning": 0.6}, fc)
    assert c2 > 0.5 and len(k2) == 2 and i2 == k2
    c3, _, _ = F.confidence({k: 1.0 for k in fc["positional_kinds"]}, fc)
    assert c3 == fc["max_confidence"]
    # the same aircraft behind two kinds is one independent signal
    st = {"sea_terminating_flight": 0.8, "cvw_aircraft": 0.6}
    c4, k4, i4 = F.confidence(st, fc, {"sea_terminating_flight": {"air:c2"},
                                       "cvw_aircraft": {"air:c2"}})
    assert len(k4) == 2 and i4 == ["sea_terminating_flight"] and c4 <= 0.45


def test_usni_candidates_and_evaluation():
    usni = {"as_of": "2026-11-16", "url": "https://news.usni.org/x", "entries": [
        {"carrier": "USS Alpha", "hull": "CVN-1", "region": "Test Sea", "status": "at_sea",
         "lat": 0.5, "lon": 9.5},
        {"carrier": "USS Beta", "hull": "CVN-2", "region": "Far Sea", "status": "at_sea",
         "lat": 40.0, "lon": -40.0},
        {"carrier": "USS Gamma", "hull": "CVN-3", "region": "Norfolk", "status": "in_port",
         "lat": 36.9, "lon": -76.3}]}
    evs = [ev("sea_terminating_flight", 0.0, 9.0, 3.0, "a"),
           ev("support_ship", 0.1, 9.1, 2.5, "s")]
    _, hyps = fuse(evs, usni)
    assert [c["name"] for c in hyps[0]["candidate_names"]] == ["USS Alpha"]
    assert "USNI" in hyps[0]["candidate_names"][0]["basis"]
    hyps[0]["id"] = "csg-abc123"
    r = F.evaluate(usni, hyps)
    assert r["available"] and r["at_sea_carriers"] == 2
    assert r["recall_with_slack"] == 0.5
    alpha = next(e for e in r["entries"] if e["carrier"] == "USS Alpha")
    assert alpha["nearest_hypothesis"] == "csg-abc123" and alpha["within_radius_plus_slack"]
    assert r["precision_confident_with_slack"] == 1.0
    assert F.evaluate(None, hyps)["available"] is False
