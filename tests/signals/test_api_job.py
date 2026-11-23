import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import h3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.signals import mount_signals
from osint.collectors.adsb_signals import gnss_observation
from osint.core.models import Observation, TrackInfo
from osint.signals import countries as countries_mod
from osint.signals.cache import write_signal
from osint.signals.gnss import cell_item
from osint.signals.job import SignalsJob, load_config
from osint.store.lake import Lake, observation_row
from osint.store.sqlite import SqliteStore

NOW = time.time()
HOT = h3.latlng_to_cell(26.0, 53.0, 3)


@pytest.fixture
def env(tmp_path):
    store, lake = SqliteStore(tmp_path / "osint.sqlite"), Lake(tmp_path / "lake")
    yield SimpleNamespace(dir=tmp_path, store=store, lake=lake)
    store.close()
    lake.close()


def seed(d):
    write_signal(d, "gnss", {
        "updated": NOW, "res": 3, "window_min": 60, "thresholds": {"min_aircraft": 3},
        "regions": [{"name": "Persian Gulf", "lat": 26.5, "lon": 52.0, "dist_nm": 250}],
        "samples": 40, "cells": [cell_item(HOT, 12, 9), cell_item(
            h3.latlng_to_cell(50.0, 9.0, 3), 20, 0)]})
    write_signal(d, "squawks", {"updated": NOW, "items": [{
        "id": f"4ca123:7700:{int(NOW) - 600}", "hex": "4ca123", "code": "7700",
        "label": "General emergency", "flight": "RYR1", "first_seen": NOW - 600,
        "last_seen": NOW - 30, "lat": 50.0, "lon": 5.0, "alt_ft": 12000, "type": "B738",
        "active": True}]})
    write_signal(d, "outages", {"updated": NOW, "window_h": 24, "countries": [
        {"cc": "IR", "name": "Iran", "lat": 32.0, "lon": 53.0, "level": "high",
         "severity": 0.7, "ongoing": True, "datasources": ["bgp"], "from": 1, "until": 2,
         "score": 40000, "events": 1}], "events": [
        {"cc": "IR", "name": "Iran", "datasource": "bgp", "method": "median",
         "from": int(NOW) - 7200, "until": int(NOW) - 60, "score": 40000.0, "weight": 1.0,
         "severity": 0.7, "ongoing": True, "lat": 32.0, "lon": 53.0},
        {"cc": "NL", "name": "Netherlands", "datasource": "merit-nt", "method": "median",
         "from": 1, "until": int(NOW), "score": 135000.0, "weight": 0.3, "severity": 0.27,
         "ongoing": True, "lat": 52.0, "lon": 5.0}]})
    ts = datetime.fromtimestamp(NOW, UTC).replace(minute=0, second=0, microsecond=0)
    write_signal(d, "radiation", {"updated": NOW, "networks": {"bfs": {"ok": True}},
                                  "stations": [
        {"id": "bfs:A", "source": "bfs", "name": "Hot", "country": "DE", "lat": 50.0,
         "lon": 10.0, "ts": ts.isoformat(), "value": 0.4, "unit": "µSv/h"},
        {"id": "bfs:B", "source": "bfs", "name": "Calm", "country": "DE", "lat": 51.0,
         "lon": 10.0, "ts": ts.isoformat(), "value": 0.1, "unit": "µSv/h"}]})
    write_signal(d, "spaceweather", {"updated": NOW, "kp_now": [NOW, 6.0], "g_now": 2,
                                     "alerts": [{"id": "K06A:1", "issued": NOW - 600,
                                                 "kind": "ALERT", "notable": True,
                                                 "headline": "Geomagnetic K-index of 6",
                                                 "scales": {"G": 2}}]})


def seed_baseline(lake):
    start = datetime.fromtimestamp(NOW, UTC) - timedelta(days=2)
    rows = []
    for i in range(24):
        t = start + timedelta(hours=i)
        for sid in ("A", "B"):
            rows.append(observation_row(Observation(
                id=f"rad:bfs:{sid}", type="radiation", version=t.isoformat(), ts=t, lat=50.0,
                lon=10.0, source="bfs", value=0.1)))
    lake.write("observations", rows)


def test_job_emits_events_alerts_and_is_idempotent(env):
    seed(env.dir)
    seed_baseline(env.lake)
    env.store.upsert_tracks([TrackInfo(track_id="air:ae0001", kind="air", ident="ae0001",
                                       name="FORTE10", type_code="RQ4", is_mil=True,
                                       high_priority=True)], datetime.now(UTC))
    job = SignalsJob(env.store, env.lake, env.dir, load_config(env.dir / "none.yaml"),
                     clock=lambda: NOW)
    out = job.run_once()
    assert out["created"] == {"squawk": 1, "gnss_jam": 1, "outage": 1, "radiation": 1}
    events = {e["subcategory"]: e for e in (env.store.get_event(i) for i in
                                            env.store.sensor_event_ids())}
    assert events["squawk"]["category"] == "air_activity"
    assert events["squawk"]["title"].startswith("Squawk 7700 (General emergency): RYR1")
    assert events["gnss_jam"]["category"] == "cyber_outage"
    assert events["gnss_jam"]["title"].startswith("GNSS interference: Persian Gulf")
    assert events["outage"]["precision"] == "country" and events["outage"]["country"] == "IR"
    assert events["radiation"]["category"] == "disaster"
    assert all(e["status"] == "sensor-detected" for e in events.values())
    summ = env.store.corroboration_summaries(list(env.store.sensor_event_ids()))
    assert not any(s["corroborated"] or s["score"] for s in summ.values())
    alerts = job.alerts
    kinds = sorted(a["kind"] for a in alerts)
    assert kinds == ["aircraft", "gnss_jam", "outage", "radiation", "space", "squawk"]
    assert job.run_once()["created"] == {} and job.run_once()["updated"] == {}
    assert len(job.alerts) == len(alerts)  # deduplicated
    # state survives a restart: no second alert for the same flight
    again = SignalsJob(env.store, env.lake, env.dir, load_config(env.dir / "none.yaml"),
                       clock=lambda: NOW + 300)
    again.run_once()
    assert sum(a["kind"] == "aircraft" for a in again.alerts) == 1


def test_job_tolerates_missing_snapshots(env):
    job = SignalsJob(env.store, env.lake, env.dir, load_config(env.dir / "none.yaml"))
    out = job.run_once()
    assert out["created"] == {} and job.alerts == []


@pytest.fixture
def client(env, monkeypatch):
    monkeypatch.setattr(countries_mod, "_cache", SimpleNamespace(
        shape=lambda cc: [[[44.0, 25.0, 63.0, 25.0, 63.0, 40.0, 44.0, 25.0]]]))
    app = FastAPI()
    mount_signals(app, SimpleNamespace(settings=SimpleNamespace(data_dir=env.dir),
                                       lake=env.lake))
    return TestClient(app)


def test_api_endpoints(client, env):
    assert client.get("/api/signals/status").json()["gnss"]["cells"] == 0  # nothing yet
    seed(env.dir)
    seed_baseline(env.lake)
    SignalsJob(env.store, env.lake, env.dir, load_config(env.dir / "none.yaml"),
               clock=lambda: NOW).run_once()
    s = client.get("/api/signals/status").json()
    assert s["gnss"]["high"] == 1 and s["squawks"]["active"] == 1
    assert s["outages"]["ongoing"] == 1 and s["spaceweather"]["g_now"] == 2
    assert s["radiation"] == {"updated": NOW, "stations": 2, "elevated": 1}
    g = client.get("/api/signals/gnss").json()
    assert g["hours"] == 1 and g["cells"][0]["h3"] == HOT and g["regions"]
    o = client.get("/api/signals/outages?shapes=1").json()
    assert o["shapes"]["IR"][0][0][:2] == [44.0, 25.0]
    assert "shapes" not in client.get("/api/signals/outages").json()
    rad = {r["id"]: r for r in client.get("/api/signals/radiation").json()["items"]}
    assert rad["bfs:A"]["status"] == "elevated" and rad["bfs:A"]["baseline"] == 0.1
    assert rad["bfs:B"]["status"] == "normal"
    al = client.get("/api/signals/alerts").json()["items"]
    assert al and client.get(f"/api/signals/alerts?since={al[0]['ts']}").json()["items"] == []
    assert client.get("/api/signals/squawks").json()["items"][0]["code"] == "7700"
    assert client.get("/api/signals/spaceweather").json()["kp_now"][1] == 6.0
    assert client.get("/api/signals/aurora").json() == {}
    sats = client.get("/api/signals/satellites?groups=stations").json()
    assert sats["items"] == [] and sats["fields"][0] == "norad"


def test_api_gnss_history_sums_closed_hours(client, env):
    hour = int(NOW // 3600 * 3600) - 3600
    env.lake.write("observations", [observation_row(o) for o in (
        gnss_observation(hour, HOT, 10, 1, 3), gnss_observation(hour - 3600, HOT, 10, 2, 3))])
    write_signal(env.dir, "gnss", {"updated": NOW, "cells": [cell_item(HOT, 5, 5)],
                                   "thresholds": {"min_aircraft": 3}})
    g = client.get("/api/signals/gnss?hours=6").json()
    c = g["cells"][0]
    assert g["hours"] == 6 and (c["n"], c["bad"]) == (25, 8) and g["hourly_rows"] == 2
