import time
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api import layers
from osint.api.m1 import mount_m1
from osint.config import Settings
from osint.core.geo import utcnow
from osint.core.models import Position, TrackInfo
from osint.runtime import Runtime

N = 50_000


def test_global_ships_layer_is_cached_and_fast(tmp_path):
    rt = Runtime(Settings(_env_file=None, data_dir=tmp_path), [])
    now = utcnow()
    live = rt.ingestor.live
    for i in range(N):
        tid = f"sea:{i}"
        live[tid] = Position(track_id=tid, kind="sea", ts=now - timedelta(seconds=i % 3600),
                             lat=(i % 170) - 85, lon=(i % 360) - 180, source="t",
                             speed_kts=12.5, heading_deg=90)
    rt.store.upsert_tracks([TrackInfo(track_id=f"sea:{i}", kind="sea", ident=str(i),
                                      name=f"SHIP {i}", type_code="70",
                                      extra={"dest": "X" * 40})
                            for i in range(0, N, 2)], now)
    app = FastAPI()
    mount_m1(app, rt)
    client = TestClient(app)
    try:
        t0 = time.perf_counter()
        r = client.get("/api/layers/ships")
        first = time.perf_counter() - t0
        assert r.status_code == 200 and r.headers["content-type"] == "application/json"
        d = r.json()
        assert d["count"] == N and set(d) == {"layer", "generated_at", "count", "items"}
        assert "extra" not in d["items"][0]
        assert first < 5.0, first  # cold: includes fetching metadata for all 50k tracks

        t0 = time.perf_counter()
        r2 = client.get("/api/layers/ships")
        second = time.perf_counter() - t0
        assert r2.content == r.content
        assert second < 0.3, second  # shared cached bytes

        t0 = time.perf_counter()
        b = client.get("/api/layers/ships?bbox=0,0,10,10").json()
        assert 0 < b["count"] < N
        assert time.perf_counter() - t0 < 1.0
        cache = app.state.layer_cache
        assert len(cache.meta) == N

        cache.ttl_s["ships"] = 0  # force a rebuild; metadata is now cached
        t0 = time.perf_counter()
        assert client.get("/api/layers/ships").json()["count"] == N
        assert time.perf_counter() - t0 < 3.0
    finally:
        rt.store.close()


def test_track_meta_cache_fetches_incrementally(tmp_path, monkeypatch):
    rt = Runtime(Settings(_env_file=None, data_dir=tmp_path), [])
    calls = []
    real = rt.store.get_tracks
    monkeypatch.setattr(rt.store, "get_tracks", lambda ids: calls.append(list(ids)) or real(ids))
    cache = layers.TrackMetaCache(rt)
    cache.get_many(["a", "b"])
    cache.get_many(["a", "b", "c"])
    assert calls == [["a", "b"], ["c"]]
    rt.store.close()
