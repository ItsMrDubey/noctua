from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api import live_ws
from osint.api.m1 import mount_m1
from osint.config import Settings
from osint.core.geo import utcnow
from osint.core.models import Observation, Position, TrackInfo
from osint.runtime import Runtime
from osint.store.lake import observation_row, position_row


def _p(tid, kind, ts, lat=10.0, lon=20.0, alt=1000):
    return Position(track_id=tid, kind=kind, ts=ts, lat=lat, lon=lon, source="t", alt_ft=alt,
                    speed_kts=300, heading_deg=90)


@pytest.fixture
def rt(tmp_path):
    r = Runtime(Settings(_env_file=None, data_dir=tmp_path), [])
    now = utcnow()
    pos = [_p("air:a", "air", now - timedelta(minutes=m), lon=20 + m / 100)
           for m in (50, 40, 30, 20)]
    pos += [_p("air:a", "air", now - timedelta(hours=10)),
            _p("air:c", "air", now - timedelta(minutes=25), lat=-30, lon=100),
            _p("sea:1", "sea", now - timedelta(minutes=5), lat=1, lon=2)]
    r.lake.write("positions", [position_row(p) for p in pos])
    r.store.upsert_tracks([
        TrackInfo(track_id="air:a", kind="air", ident="a", name="RCH1", is_mil=True),
        TrackInfo(track_id="air:c", kind="air", ident="c", name="CIV", is_mil=False),
    ], now)
    r.ingestor.live["air:a"] = _p("air:a", "air", now, lon=21)
    r.ingestor.live["sea:1"] = _p("sea:1", "sea", now - timedelta(minutes=5), lat=1, lon=2)
    obs = [Observation(id=f"fire:{i}", type="fire", ts=now - timedelta(hours=1), lat=5, lon=6,
                       source="f", value=1.0) for i in range(3)]
    obs.append(Observation(id="fire:far", type="fire", ts=now - timedelta(hours=1), lat=-40,
                           lon=100, source="f", value=1.0))
    obs.append(Observation(id="fire:old", type="fire", ts=now - timedelta(hours=5), lat=5,
                           lon=6, source="f", value=1.0))
    r.lake.write("observations", [observation_row(o) for o in obs])
    yield r
    r.store.close()


@pytest.fixture
def client(rt):
    app = FastAPI()
    mount_m1(app, rt)
    return TestClient(app)


def test_trail_ordered_with_live_point(client):
    d = client.get("/api/tracks/air:a?hours=6").json()
    ts = [p["ts"] for p in d["points"]]
    assert d["track_id"] == "air:a" and len(ts) == 5  # 4 lake + live, 10h-old excluded
    assert ts == sorted(ts)
    assert d["points"][-1]["lon"] == 21
    assert len(client.get("/api/tracks/air:a?hours=48").json()["points"]) == 6
    assert client.get("/api/tracks/air:a?hours=49").status_code == 422
    assert client.get("/api/tracks/nope").json()["points"] == []


def test_multi_tracks_mil_only(client):
    d = client.get("/api/tracks?kind=air&hours=1&mil_only=true").json()["tracks"]
    assert list(d) == ["air:a"] and len(d["air:a"]) == 4
    assert len(d["air:a"][0]) == 4
    allt = client.get("/api/tracks?kind=air&hours=1").json()["tracks"]
    assert set(allt) == {"air:a", "air:c"}


def test_downsample():
    from osint.api.history import _downsample
    out = _downsample(list(range(500)), 60)
    assert len(out) <= 60 and out[0] == 0 and out[-1] == 499


def test_snapshot_latest_within_window(client):
    at = (utcnow() - timedelta(minutes=15)).isoformat()
    d = client.get("/api/snapshot", params={"at": at, "kind": "air", "window_min": 3}).json()
    assert d["count"] == 0
    d = client.get("/api/snapshot", params={"at": at, "kind": "air", "window_min": 30}).json()
    by = {i["id"]: i for i in d["items"]}
    assert set(by) == {"air:a", "air:c"}
    assert by["air:a"]["lon"] == pytest.approx(20.2)  # latest <= at is the 20-min-ago point
    assert by["air:a"]["name"] == "RCH1" and by["air:a"]["is_mil"] is True
    sea = client.get("/api/snapshot", params={"at": utcnow().isoformat(), "kind": "sea"}).json()
    assert sea["layer"] == "ships" and sea["count"] == 1
    assert client.get("/api/snapshot", params={"at": "garbage"}).status_code == 422


def test_until_param(client):
    until = (utcnow() - timedelta(hours=2)).isoformat()
    assert client.get("/api/layers/fires", params={"hours": 24}).json()["count"] == 5
    assert client.get("/api/layers/fires", params={"hours": 24, "until": until}).json()[
        "count"] == 1  # only the 5h-old fire is <= until
    assert client.get("/api/layers/quakes", params={"until": "bad"}).status_code == 422


def test_density(client):
    d = client.get("/api/density?layer=fires&res=3&hours=24").json()
    assert d["res"] == 3 and sorted(c["count"] for c in d["cells"]) == [1, 4]
    assert all(len(c["boundary"]) >= 5 and len(c["boundary"][0]) == 2 for c in d["cells"])
    assert client.get("/api/density?layer=fires&res=3&hours=2").json()["cells"][0]["count"] >= 1
    d4 = client.get("/api/density?layer=fires&res=4").json()
    assert sum(c["count"] for c in d4["cells"]) == 5
    d5 = client.get("/api/density?layer=aircraft&res=5&hours=24").json()
    assert sum(c["count"] for c in d5["cells"]) == 6
    assert client.get("/api/density?layer=bogus").status_code == 422


def test_websocket_snapshot_then_delta(rt, monkeypatch):
    monkeypatch.setattr(live_ws, "INTERVAL_S", 0.05)
    app = FastAPI()
    mount_m1(app, rt)
    with TestClient(app).websocket_connect("/ws/live") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and snap["removed"] == []
        assert [i["id"] for i in snap["aircraft"]] == ["air:a"] and snap["aircraft"][0]["is_mil"]
        assert [i["id"] for i in snap["ships"]] == ["sea:1"]
        d = ws.receive_json()
        assert d["type"] == "delta" and d["aircraft"] == [] and d["ships"] == []
        now = utcnow()
        with rt.ingestor._lock:
            rt.ingestor.live["air:c"] = _p("air:c", "air", now, lat=3)
            rt.ingestor.live["air:a"] = _p("air:a", "air", now + timedelta(seconds=1))
            del rt.ingestor.live["sea:1"]
        for _ in range(5):
            d = ws.receive_json()
            if d["aircraft"] or d["removed"]:
                break
        assert {i["id"] for i in d["aircraft"]} == {"air:a", "air:c"}
        assert d["removed"] == ["sea:1"]
        assert next(i for i in d["aircraft"] if i["id"] == "air:c")["name"] == "CIV"


def test_history_counts_lake_and_events(client, rt):
    now = utcnow()
    q = f"start={(now - timedelta(hours=6)).isoformat()}&end={(now + timedelta(minutes=1)).isoformat()}"
    q = q.replace("+", "%2B")
    j = client.get(f"/api/history/counts?layer=fires&bin=hour&{q}").json()
    assert sum(n for _, n in j["counts"]) == 5 and all(t % 3600000 == 0 for t, _ in j["counts"])
    assert len(j["counts"]) >= 1 and j["bin_s"] == 3600
    d = client.get(f"/api/history/counts?layer=fires&bin=day&{q}").json()
    assert sum(n for _, n in d["counts"]) == 5 and all(t % 86400000 == 0 for t, _ in d["counts"])
    a = client.get(f"/api/history/counts?layer=aircraft&{q}").json()
    assert sum(n for _, n in a["counts"]) >= 2  # distinct tracks per bin
    assert client.get("/api/history/counts?layer=quakes").json()["counts"] == []
    rt.store.save_events([
        {**_ev("h1", "multi-source", now - timedelta(hours=2)), "lat": 1.0, "lon": 1.0},
        {**_ev("h2", "historical", now - timedelta(hours=2)), "lat": 1.0, "lon": 1.0},
    ])
    e = client.get(f"/api/history/counts?layer=events&{q}").json()
    h = client.get(f"/api/history/counts?layer=historical&{q}").json()
    assert sum(n for _, n in e["counts"]) == 1 and sum(n for _, n in h["counts"]) == 1
    assert client.get("/api/history/counts?layer=nope").status_code == 422
    assert client.get("/api/history/counts?layer=aircraft&start=2020-01-01T00:00:00Z").status_code == 422
    assert client.get("/api/history/counts?layer=fires&start=junk").status_code == 422


def _ev(eid, status, t):
    from osint.store.sqlite import iso

    return {"id": eid, "category": "armed_conflict", "title": eid, "summary": "", "actors": [],
            "t_start": iso(t), "t_end": iso(t), "status": status, "confidence": 0.9, "version": 1,
            "mention_count": 1, "source_diversity": 1, "gdelt_ids": [], "first_seen": iso(t),
            "updated_at": iso(t), "cluster_state": {}}


def test_history_counts_cache_serves_closed_bins(rt, monkeypatch):
    from osint.api import history

    history._cache.clear()
    now = utcnow()
    t0, t1 = now - timedelta(hours=8), now + timedelta(minutes=1)
    first = history.history_counts(rt, "fires", "hour", t0, t1)
    calls = []
    real = history._lake_counts
    monkeypatch.setattr(history, "_lake_counts", lambda *a, **k: calls.append(a[3:]) or real(*a, **k))
    again = history.history_counts(rt, "fires", "hour", t0, t1)
    assert again["counts"] == first["counts"] and sum(n for _, n in first["counts"]) == 5
    assert len(calls) == 1  # only the open tail hits the lake
    assert calls[0][0] >= now - timedelta(hours=2)
