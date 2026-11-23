from osint.timeline.build import build_timeline


def _event(fields=(), docs=None):
    docs = docs if docs is not None else [
        {"doc_id": "d1", "source": "rss:kyiv-independent", "url": "https://k.example/a",
         "title": "Refinery burning", "published_at": "2026-11-23T01:18:52+00:00", "role": "primary",
         "language": "en"},
        {"doc_id": "d2", "source": "telegram:kyiv", "url": "https://t.me/k/1",
         "title": "Refinery burning", "published_at": "2026-11-23T01:21:30+00:00", "role": "mention",
         "language": "en"}]
    return {"id": "E1", "title": "Refinery burning", "lat": 47.76, "lon": 39.93,
            "t_start": "2026-11-23T01:18:52+00:00", "first_seen": "2026-11-23T04:29:23+00:00",
            "documents": docs, "fields": list(fields)}


FIRES = [{"kind": "fire", "ref_id": f"fire:{i}", "rule": "firms_hotspot", "distance_km": 8 + i,
          "score": 0.5 - i / 10, "created_at": "2026-11-23T04:31:52+00:00",
          "details": {"frp_mw": 300 - i, "lat": 47.83, "lon": 39.86, "satellite": "N21",
                      "ts": "2026-11-23T00:05:00+00:00"}} for i in range(3)] + [
    {"kind": "fire", "ref_id": "fire:x", "rule": "firms_hotspot", "distance_km": 9.2, "score": 0.3,
     "created_at": "2026-11-23T04:40:00+00:00",
     "details": {"frp_mw": 9.9, "lat": 47.83, "lon": 39.86, "satellite": "N20",
                 "ts": "2026-11-22T23:20:00+00:00"}}]


def test_kinds_order_flags_and_relative_times():
    tl = build_timeline(
        _event([{"field": "when", "value": "early on Nov. 23", "doc_id": "d1", "quote": ""},
                {"field": "time", "value": "2026-11-23T01:18:52", "doc_id": "d1"}]),
        corroborations=FIRES,
        imagery=[{"role": "before", "status": "ok", "acquired_at": "2026-10-26T08:26:55+00:00",
                  "collection": "sentinel-2-l2a", "scene_id": "S", "cloud_pct": 0.1},
                 {"role": "after", "status": "awaiting_after", "acquired_at": None}],
        status_history=[{"status": "reported", "prev_status": None, "at": "2026-11-23T04:29:23+00:00",
                         "source": "insert"},
                        {"status": "sensor-corroborated", "prev_status": "reported",
                         "at": "2026-11-23T04:31:53+00:00", "source": "change"}])
    kinds = [(e["kind"], e.get("sensor")) for e in tl["entries"]]
    # baseline image first, then the claimed band, both FIRMS passes, the two reports, system rows
    assert kinds[:5] == [("observed", "imagery"), ("occurred", None), ("observed", "fire"),
                         ("observed", "fire"), ("reported", None)]
    fires = [e for e in tl["entries"] if e.get("sensor") == "fire"]
    assert [f["count"] for f in fires] == [1, 3]  # one row per satellite scan
    assert "first_sensor" in fires[0]["flags"] and fires[0]["t_rel_min"] < -118
    rep = [e for e in tl["entries"] if e["kind"] == "reported"]
    assert rep[0]["flags"] == ["first_report"] and rep[0]["t_rel_min"] == 0 and rep[1]["t_rel_min"] == 2.6
    status = [e["label"] for e in tl["entries"] if e.get("milestone") == "status"]
    assert status == ["Status reported (first recorded)", "Status reported → sensor-corroborated"]
    assert tl["status_history_recorded"]
    assert not any("first_imagery" in e["flags"] for e in tl["entries"])  # no after image yet
    assert any(e.get("milestone") == "sensor_linked" for e in tl["entries"])
    occ = tl["occurred"]
    assert occ["basis"] == "claims" and occ["precision"] == "part_of_day"
    assert occ["hi"] == "2026-11-23T01:18:00+00:00"  # clamped to the publication minute
    assert tl["local_offset_h"] == 3.0


def test_conflicting_claims_are_side_by_side_not_resolved():
    tl = build_timeline(_event([{"field": "when", "value": "Nov 20", "doc_id": "d1"},
                                {"field": "when", "value": "on November 22", "doc_id": "d2"}]))
    occ = tl["occurred"]
    assert occ["conflicts"] == [["d1", "d2"]] and occ["lo"] is None
    claims = [e for e in tl["entries"] if e["kind"] == "occurred"]
    assert all("conflict" in c["flags"] for c in claims) and len(claims) == 2


def test_agreeing_claims_merge_and_unplaced_and_no_claim():
    tl = build_timeline(_event([{"field": "when", "value": "Nov 22", "doc_id": "d1"},
                                {"field": "when", "value": "22 листопада", "doc_id": "d2"},
                                {"field": "when", "value": "in recent weeks", "doc_id": "d2"}]))
    claims = [e for e in tl["entries"] if e["kind"] == "occurred"]
    assert len(claims) == 1 and claims[0]["also"][0]["doc_id"] == "d2"
    assert [u["phrase"] for u in tl["unplaced"]] == ["in recent weeks"]
    tl = build_timeline(_event())
    assert tl["occurred"]["basis"] == "before_first_report"
    assert tl["occurred"]["hi"] == "2026-11-23T01:18:52+00:00"
    assert not tl["status_history_recorded"]


def test_after_imagery_flagged_and_baseline_status_labelled():
    tl = build_timeline(_event(), imagery=[
        {"role": "after", "status": "ok", "acquired_at": "2026-11-25T08:20:00+00:00",
         "collection": "sentinel-2-l2a", "scene_id": "A", "cloud_pct": 3}],
        status_history=[{"status": "multi-source", "prev_status": None,
                         "at": "2026-11-24T00:00:00+00:00", "source": "baseline"}])
    img = next(e for e in tl["entries"] if e.get("sensor") == "imagery")
    assert img["flags"] == ["first_imagery"] and img["label"] == "Post-event imagery"
    st = next(e for e in tl["entries"] if e.get("milestone") == "status")
    assert st["baseline"] and "earlier changes not recorded" in st["label"]
    assert not tl["status_history_recorded"]
