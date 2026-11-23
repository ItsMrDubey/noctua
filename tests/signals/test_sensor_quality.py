"""Sensor-derived event quality: own-sensor evidence is detection (not corroboration), GNSS
confidence from the detection statistics with a lifecycle, squawk persistence, IODA
labelling, radiation provisional status, and the one-off migration of stored events."""

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import h3
import pytest

from osint.core.sensor_status import SENSOR_DETECTED, detection_kinds
from osint.intel.aggregate import cell_size_text, sensor_assessment
from osint.signals import gnss as gnss_mod
from osint.signals.aviation import SquawkTracker, persistent, reports
from osint.signals.cache import write_signal
from osint.signals.gnss import GnssAggregator, NavSample, cell_item, wilson
from osint.signals.job import (
    SignalsJob,
    count_windows,
    gnss_reading,
    gnss_summary,
    legacy_gnss_details,
    load_config,
    merge_gnss,
)
from osint.signals.outages import confirmed, country_label
from osint.signals.radiation import assess_detail
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore
from osint.timeline.build import build_timeline

CFG = load_config(__import__("pathlib").Path("/nonexistent"))
CELL = h3.latlng_to_cell(25.48, 55.78, 3)
T0 = datetime(2026, 11, 23, 4, 50, tzinfo=UTC)


@pytest.fixture
def env(tmp_path):
    store, lake = SqliteStore(tmp_path / "osint.sqlite"), Lake(tmp_path / "lake")
    yield SimpleNamespace(dir=tmp_path, store=store, lake=lake)
    store.close()
    lake.close()


def reading(t, n, bad, cells=(CELL,)):
    a = gnss_mod.assess(n, bad, 1)
    return {"reading": {"t": t.isoformat(), "window_min": 60, "n": n, "bad": bad,
                        "pct": round(100 * bad / n, 1), "cells": list(cells),
                        "wilson_lo": a["wilson_lo"], "wilson_hi": a["wilson_hi"]},
            "ac": [], "region": "Persian Gulf"}


# ------------------------------------------------------------------ statistics


def test_wilson_interval_and_small_sample_wording():
    lo, hi = wilson(4, 11)
    assert 0.15 < lo < 0.16 and 0.64 < hi < 0.65
    small = gnss_mod.assess(11, 4, 1)  # the live example: 4 of 11, one window
    assert small["wording"] == "possible" and small["confidence"] < 0.6
    assert any("11 aircraft" in x for x in small["limits"])
    big = gnss_mod.assess(44, 11, 3)
    assert big["wording"] == "likely" and big["confidence"] > small["confidence"]
    assert gnss_mod.assess(44, 11, 1)["wording"] == "possible"  # persistence required


def test_count_windows_non_overlapping():
    ts = [(T0 + timedelta(minutes=m)).isoformat() for m in (0, 10, 50, 70, 200)]
    assert count_windows(ts, 60) == 3  # 0, 70, 200


# ------------------------------------------------------------------ lifecycle


def test_gnss_lifecycle_detection_peak_now_and_summary():
    d = merge_gnss(None, reading(T0, 11, 4), CFG["gnss_jam"])
    assert d["lifecycle"] == "active" and d["detection"]["n"] == 11
    d = merge_gnss(d, reading(T0 + timedelta(minutes=80), 40, 16), CFG["gnss_jam"])
    assert d["peak"]["n"] == 40 and d["detection"]["n"] == 11
    assert d["assessment"]["windows"] == 2
    # 5 h later only 1 of 16 degraded: subsiding (above medium_pct), with its own time
    cell = cell_item(CELL, 16, 1)
    later = T0 + timedelta(hours=5, minutes=20)
    d = gnss_reading(d, [cell], later, CFG["gnss_jam"])
    assert d["lifecycle"] == "subsiding" and d["latest"]["pct"] == 6.2
    s = gnss_summary(d)
    assert "At detection 23 Nov 04:50Z" in s and "4 of 11 aircraft (36%)" in s
    assert "Now 23 Nov 10:10Z: 1 of 16 aircraft (6%) degraded - subsiding" in s
    assert "Peak 23 Nov 06:10Z" in s
    # nothing degraded any more, more than a window after the last hot reading: ended
    d = gnss_reading(d, [cell_item(CELL, 20, 0)], later + timedelta(minutes=10),
                     CFG["gnss_jam"])
    assert d["lifecycle"] == "ended"
    assert datetime.fromisoformat(d["ended_at"]) == T0 + timedelta(minutes=80)
    assert [s[3] for s in d["series"]] == [True, True, False, False]


def test_job_gnss_event_is_sensor_detected_and_tracks_lifecycle(env):
    now = time.time()
    hot = [cell_item(CELL, 12, 5)]
    write_signal(env.dir, "gnss", {"updated": now, "window_min": 60, "cells": hot,
                                   "regions": [{"name": "Persian Gulf", "lat": 26.5,
                                                "lon": 52.0, "dist_nm": 250}]})
    job = SignalsJob(env.store, env.lake, env.dir, CFG, clock=lambda: now)
    assert job.run_once()["created"] == {"gnss_jam": 1}
    eid = next(iter(env.store.sensor_event_ids()))
    ev = env.store.get_event(eid)
    assert ev["status"] == SENSOR_DETECTED and ev["summary"].startswith("Possible GNSS")
    summ = env.store.corroboration_summaries([eid])[eid]
    assert summ["corroborated"] is False and summ["score"] == 0.0
    rows = env.store.list_corroborations(eid)
    assert rows[0]["details"]["ts"] and rows[0]["distance_km"] == 0.0
    # 20 min later the cell is quiet: latest reading recorded, lifecycle subsiding
    later = now + 1200
    write_signal(env.dir, "gnss", {"updated": later, "window_min": 60,
                                   "cells": [cell_item(CELL, 14, 0)], "regions": []})
    job.clock = lambda: later
    out = job.run_once()
    assert out["gnss_lifecycle"] == 1
    sensor = env.store.get_sensor_event(eid)["details"]
    assert sensor["lifecycle"] == "subsiding" and sensor["latest"]["n"] == 14
    assert "Now " in env.store.get_event(eid)["summary"]


def test_aggregator_lists_degraded_aircraft_with_integrity():
    agg = GnssAggregator({"min_aircraft": 2})
    t = time.time()
    agg.add([NavSample("aa0001", 25.5, 55.8, t, True, 0, 0, False),
             NavSample("aa0002", 25.5, 55.8, t, False, 8, 9, False)])
    cell = agg.snapshot(t)[0]
    assert cell["bad"] == 1 and cell["ac"][0] == {
        "hex": "aa0001", "bad": True, "carried": False, "seen": round(t), "nic": 0,
        "nac_p": 0, "gps_lost": False}


# ------------------------------------------------------------------ squawk persistence


def test_squawk_tracker_counts_reports_and_persistence():
    tr = SquawkTracker()
    ac = {"hex": "3c6445", "squawk": "7500", "flight": "DLH15C", "lat": 49.98, "lon": 8.36,
          "seen_pos": 0}
    tr.update([ac, ac], 1000.0)  # same report twice in one poll: one report
    ep = tr.items(1000.0)[0]
    assert ep["reports"] == 1 and not ep["persistent"]
    tr.update([ac], 1030.0)
    ep = tr.items(1030.0)[0]
    assert ep["reports"] == 2 and ep["persistent"]
    # cursor episodes from before the counter
    assert reports({"first_seen": 5, "last_seen": 5}) == 1
    assert not persistent({"first_seen": 5, "last_seen": 5})


def test_one_report_squawk_is_no_event_only_a_transient_note(env):
    now = time.time()
    ep = {"id": f"3c6445:7500:{int(now) - 900}", "hex": "3c6445", "code": "7500",
          "label": "Unlawful interference", "flight": "DLH15C", "first_seen": now - 900,
          "last_seen": now - 900, "reports": 1, "lat": 49.9858, "lon": 8.3607,
          "alt_ft": 2525, "squawk": "1000", "active": False}
    write_signal(env.dir, "squawks", {"updated": now, "items": [ep]})
    job = SignalsJob(env.store, env.lake, env.dir, CFG, clock=lambda: now)
    job.alerts = [{"id": f"squawk:{ep['id']}", "ts": now - 800, "sev": "high",
                   "kind": "squawk", "title": "old alert"}]  # raised by an earlier build
    out = job.run_once()
    assert out["created"] == {} and not env.store.sensor_event_ids()
    sq = [a for a in job.alerts if a["kind"] == "squawk"]
    assert len(sq) == 1 and sq[0]["sev"] == "info" and sq[0]["transient"]
    assert sq[0]["title"].startswith("Transient squawk 7500 (1 report)")


# ------------------------------------------------------------------ IODA / radiation


def test_ioda_telescope_only_is_an_unconfirmed_anomaly_with_iso_times():
    row = {"cc": "AE", "level": "low", "ongoing": True, "datasources": ["merit-nt"],
           "from": 1795341540, "until": 1795428837}
    lab = country_label(row)
    assert lab["label"] == "telescope-only anomaly (unconfirmed)" and not lab["confirmed"]
    assert lab["since"] == "2026-11-22T09:59:00+00:00"  # seconds, not milliseconds
    assert confirmed(["merit-nt", "bgp"]) and not confirmed(["merit-nt"])
    assert country_label({**row, "datasources": ["bgp"]})["label"] == "outage (low)"
    assert country_label(None)["label"] == "normal"


def test_radiation_status_with_short_history_is_provisional():
    assert assess_detail(0.1, [0.09, 5]) == ("normal", True)
    assert assess_detail(0.5, [0.1, 5]) == ("elevated", True)
    assert assess_detail(0.1, [0.09, 50]) == ("normal", False)
    assert assess_detail(0.1, None) == ("unknown", False)


# ------------------------------------------------------------------ assessment / timeline


def _gnss_event(details):
    return {"id": "E1", "status": SENSOR_DETECTED, "confidence": 0.51, "precision": "point",
            "h3_3": CELL, "lat": 25.48, "lon": 55.78, "t_start": T0.isoformat(),
            "t_end": (T0 + timedelta(hours=1)).isoformat(), "documents": [], "fields": [],
            "corroborations": [{"kind": "gnss_cell", "ref_id": CELL, "rule": "gnss_jam",
                                "distance_km": 0.0, "score": 0.36,
                                "details": {"aircraft": 11, "degraded": 4, "pct": 36.4,
                                            "ts": T0.isoformat(), "window_min": 60},
                                "created_at": T0.isoformat()},
                               {"kind": "fire", "ref_id": "f1", "rule": "x",
                                "distance_km": 22.0, "score": 0.2, "details": {}}],
            "sensor": {"detector": "gnss_jam", "signal": True,
                       "sensor": "ADS-B navigation integrity (NIC/NACp)",
                       "details": details}}


def test_sensor_assessment_never_self_corroborates_and_labels_the_cell():
    d = merge_gnss(None, reading(T0, 11, 4), CFG["gnss_jam"])
    ev = _gnss_event(d)
    ev["corroborations"] = ev["corroborations"][:1]
    tl = build_timeline(ev, corroborations=ev["corroborations"], sensor=ev["sensor"])
    items = sensor_assessment(ev, {"timeline": tl})
    text = " | ".join(x["text"] for x in items)
    assert not any(x["level"] == "corroborated" for x in items)
    assert "no news reports" in text and "single outlet" not in text
    assert "No independent corroboration" in text
    assert cell_size_text(3) in text and "place centroid" not in text
    assert "Possible interference" in text
    # a different sensor kind near the event is independent evidence
    ev2 = _gnss_event(d)
    items2 = sensor_assessment(ev2, {})
    assert any(x["level"] == "corroborated" and "1 fire" in x["text"] for x in items2)
    assert detection_kinds("gnss_jam") == {"gnss_cell"}


def test_sensor_timeline_occurred_is_the_detection_windows_with_times():
    d = merge_gnss(None, reading(T0, 11, 4), CFG["gnss_jam"])
    d = merge_gnss(d, reading(T0 + timedelta(minutes=90), 30, 9), CFG["gnss_jam"])
    ev = _gnss_event(d)
    imagery = [{"role": "before", "status": "ok", "acquired_at": "2026-11-15T07:02:32+00:00"}]
    tl = build_timeline(ev, corroborations=ev["corroborations"][:1], imagery=imagery,
                        sensor=ev["sensor"])
    occ = tl["occurred"]
    assert occ["basis"] == "sensor" and occ["windows"] == [
        ["2026-11-23T03:50:00+00:00", "2026-11-23T04:50:00+00:00"],
        ["2026-11-23T05:20:00+00:00", "2026-11-23T06:20:00+00:00"]]
    assert tl["anchor"]["first_detection"] == "2026-11-23T04:50:00+00:00"
    assert all(e.get("t") for e in tl["entries"])  # no "–" rows
    assert not any(e.get("sensor") == "imagery" for e in tl["entries"])
    labels = [e["label"] for e in tl["entries"] if e["kind"] == "observed"]
    # latest == peak here: one row, not two
    assert labels[0] == "GNSS interference detected" and "Peak reading" in labels


# ------------------------------------------------------------------ migration


def _legacy(store, eid, detector, key, status="sensor-corroborated", t_start=T0, t_end=T0,
            details=None, kind="aircraft"):
    store.save_sensor_event(
        {"id": eid, "category": "cyber_outage", "subcategory": detector, "cameo_root": None,
         "title": eid, "summary": "old", "actors": [], "lat": 25.48, "lon": 55.78,
         "precision": "point", "place_name": "x", "country": None, "geo_confidence": 1.0,
         "h3_3": CELL, "h3_5": h3.latlng_to_cell(25.48, 55.78, 5),
         "h3_7": h3.latlng_to_cell(25.48, 55.78, 7), "t_start": t_start.isoformat(),
         "t_end": t_end.isoformat(), "status": status, "confidence": 0.74, "version": 1,
         "mention_count": 1, "source_diversity": 0, "gdelt_ids": [],
         "first_seen": t_start.isoformat(), "updated_at": t_start.isoformat(),
         "cluster_state": {}},
        {"detector": detector, "dedupe_key": key, "cell": CELL, "details": details or {}},
        [{"event_id": eid, "kind": kind, "ref_id": CELL, "rule": detector,
          "distance_km": 0.0, "dt_minutes": 0.0, "score": 0.3, "details": {}}],
        {"event_id": eid, "score": 0.74, "kinds": [kind], "evidence": 1,
         "corroborated": True})


def test_migration_is_idempotent_and_respects_the_rebuild_lock(env):
    from osint.pipeline.events import rebuild_lock_path
    from osint.store.lake import FileLock

    s = env.store
    _legacy(s, "GNSS", "gnss_jam", "gnss_jam:x", t_end=T0 + timedelta(hours=4),
            details={"aircraft": 11, "degraded": 4, "pct": 36.4}, kind="gnss_cell")
    _legacy(s, "BLIP", "squawk", "squawk:blip")  # one report: first == last seen
    _legacy(s, "LONG", "squawk", "squawk:long", t_end=T0 + timedelta(minutes=10),
            details={"reports": 7})
    _legacy(s, "ORBIT", "orbit", "orbit:x", kind="aircraft")
    _legacy(s, "HAZ", "hazard_flood", "hazard:x", status="official", kind="hazard")
    job = SignalsJob(s, env.lake, env.dir, CFG, clock=lambda: time.time())
    with FileLock(rebuild_lock_path(s.path), timeout=1):
        assert job.migrate() is None  # a rebuild holds the lock: skipped, nothing changed
    assert s.get_event("GNSS")["status"] == "sensor-corroborated"
    assert job.migrate() == {"status": 4, "squawk_removed": 1, "gnss_reassessed": 1}
    assert s.get_event("BLIP") is None
    assert {e: s.get_event(e)["status"] for e in ("GNSS", "LONG", "ORBIT", "HAZ")} == {
        "GNSS": SENSOR_DETECTED, "LONG": SENSOR_DETECTED, "ORBIT": SENSOR_DETECTED,
        "HAZ": "official"}
    g = s.get_event("GNSS")
    assert g["confidence"] < 0.74 and "reading not recorded" in g["summary"]
    assert "Last hot reading 23 Nov 08:50Z: 4 of 11" in g["summary"]
    assert s.corroboration_summaries(["ORBIT"])["ORBIT"]["corroborated"] is False
    assert s.corroboration_summaries(["HAZ"])["HAZ"]["corroborated"] is True
    assert job.migrate() == {"status": 0, "squawk_removed": 0, "gnss_reassessed": 0}


def test_legacy_details_keep_what_is_known():
    ev = {"t_start": T0.isoformat(), "t_end": (T0 + timedelta(hours=4)).isoformat(),
          "h3_3": CELL}
    d = legacy_gnss_details({"cell": CELL, "t_end": ev["t_end"],
                             "details": {"aircraft": 11, "degraded": 4}}, ev,
                            CFG["gnss_jam"], ["8343a1fffffffff"])
    assert d["detection"]["unrecorded"] and d["peak"]["n"] == 11
    assert d["cells"] == sorted({CELL, "8343a1fffffffff"})
    assert d["assessment"]["windows"] == 2


async def test_deferred_migration_is_retried_until_it_runs(env, monkeypatch):
    # the event builder holds the rebuild lock on every pass: a migration deferred at
    # start-up must run on a later pass, and only once
    job = SignalsJob(env.store, env.lake, env.dir, CFG, clock=lambda: time.time())
    outcomes = iter([None, {"status": 2, "squawk_removed": 1, "gnss_reassessed": 0}])
    calls = []
    monkeypatch.setattr(job, "migrate", lambda *a: calls.append(a) or next(outcomes))
    await job._try_migrate()
    assert not job._migrated
    await job._try_migrate()
    assert job._migrated
    await job._try_migrate()
    assert len(calls) == 2
