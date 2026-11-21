"""Benchmark API: envelopes, validation, history, sample, labels round-trip."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.benchmark import mount_benchmark
from tests.benchmark.fixtures import NOW, build, eid, service


class _RT:
    pass


@pytest.fixture
def ctx(tmp_path):
    built = build(tmp_path)
    rt = _RT()
    rt.benchmark = service(tmp_path, built["lake"])
    app = FastAPI()
    mount_benchmark(app, rt)
    yield TestClient(app), rt.benchmark
    rt.benchmark.store.close()
    built["lake"].close()


def test_scorecard_envelope_and_cache(ctx):
    client, svc = ctx
    r = client.get("/api/benchmark/scorecard?window=24h")
    assert r.status_code == 200 and r.headers["content-type"] == "application/json"
    body = r.json()
    assert body["window"] == "24h" and body["as_of"].startswith("2026-11-21")
    assert body["metrics"]["duplication"]["definition"]
    assert client.get("/api/benchmark/scorecard").json()["compute_s"] == body["compute_s"]
    assert svc._cache["24h"][1] is not None
    assert client.get("/api/benchmark/scorecard?window=30d").status_code == 422


def test_history(ctx):
    client, svc = ctx
    assert client.get("/api/benchmark/history").json()["series"] == {}
    svc.snapshot()
    r = client.get("/api/benchmark/history?metric=duplication.gdelt.rows_per_incident&days=7")
    body = r.json()
    assert body["metrics"] == ["duplication.gdelt.rows_per_incident"]
    assert body["series"]["duplication.gdelt.rows_per_incident"][0]["value"] == 2.0
    everything = client.get("/api/benchmark/history?window=7d").json()
    assert len(everything["metrics"]) > 20
    assert client.get("/api/benchmark/history?days=0").status_code == 422


def test_sample(ctx):
    client, _ = ctx
    body = client.get("/api/benchmark/sample?n=50&seed=1").json()
    ids = {e["id"] for e in body["events"]}
    assert len(ids) == body["n"] == 6  # every 24h event (S1 included), no duplicates
    e1 = next(e for e in body["events"] if e["id"] == eid("E1"))
    assert len(e1["documents"]) == 2 and e1["verdict"] == "supported"
    assert {g["gdelt_event_id"] for g in e1["gdelt_attached"]} == {"g1", "g2", "g3"}
    assert e1["gdelt_attached"][0]["geo_type"] in (1, 4)
    rej = body["gdelt_rejected"]
    assert {r["reason"] for r in rej} == {"irrelevant_category", "stale",
                                          "no_article_backing", "no_location"}
    g7 = next((r for r in rej if r["gdelt_event_id"] == "g7"), None)
    assert g7 is None or g7["num_mentions"] == 66
    small = client.get("/api/benchmark/sample?n=2&seed=3").json()
    assert small["n"] == 2
    assert client.get("/api/benchmark/sample?n=2&seed=3").json() == small  # seeded


def test_labels_round_trip(ctx):
    client, _svc = ctx
    r = client.post("/api/benchmark/labels", json={
        "event_id": eid("E1"), "correct_event": True, "correct_location": False,
        "notes": "Kyiv oblast, not city"})
    assert r.status_code == 200
    assert r.json()["label"]["correct_location"] is False
    assert r.json()["summary"]["status"] == "insufficient labels"
    r = client.post("/api/benchmark/labels", json={
        "event_id": eid("E1"), "correct_event": True, "correct_location": True})
    listing = client.get("/api/benchmark/labels").json()
    assert listing["count"] == 1 and listing["items"][0]["correct_location"] is True
    assert client.post("/api/benchmark/labels", json={
        "event_id": "nope", "correct_event": True,
        "correct_location": True}).status_code == 404
    assert client.post("/api/benchmark/labels", json={"event_id": eid("E1")}).status_code == 422
    sample = client.get("/api/benchmark/sample?n=50").json()
    e1 = next(e for e in sample["events"] if e["id"] == eid("E1"))
    assert e1["label"]["correct_location"] is True


def test_labels_enough_for_quality(ctx):
    _, svc = ctx
    for i in range(20):
        svc.store.save_label(f"x{i}", correct_event=True, correct_location=i < 15, now=NOW)
    q = svc.compute("24h")["metrics"]["quality"]
    assert q["status"] == "ok" and q["precision_at_location_pct"] == 75.0
    assert q["by_precision"]["unknown"]["labels"] == 20
