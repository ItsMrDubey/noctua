"""StorylineJob against a real SqliteStore (incremental / full passes, stable ids,
redirects) and the storylines API + intel section on its output."""

import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.intel import mount_intel
from osint.api.storylines import mount_storylines
from osint.store.sqlite import SqliteStore, iso
from osint.storylines.config import StorylineConfig
from osint.storylines.job import StorylineJob, assign_ids
from osint.storylines.store import StorylineStore
from tests.storylines.fixtures import T0, as_row, benghazi_thread, finland_lookalike, node

NOW = T0 + timedelta(days=2)


def _docs(n, k=2):
    return [{"event_id": n.id, "doc_id": f"{n.id}-d{i}", "source": f"rss:outlet{i}",
             "url": f"https://o{i}.example/{n.id}", "title": n.title,
             "published_at": iso(n.t + timedelta(minutes=7 * i)), "language": "en",
             "role": "primary" if i == 0 else "mention", "added_at": iso(n.t)}
            for i in range(k)]


class FakeLake:
    def query(self, sql, params=None, start=None, end=None):
        return []


@pytest.fixture
def rt(tmp_path):
    store = SqliteStore(tmp_path / "osint.sqlite")
    nodes = benghazi_thread() + finland_lookalike()
    store.save_events([as_row(n, now=NOW) for n in nodes],
                      documents=[d for n in nodes for d in _docs(n)],
                      fields=[{"event_id": "E1", "field": "when", "value": "late on November 21",
                               "doc_id": "E1-d0", "quote": "late on Saturday"}])
    store.save_corroborations([{
        "event_id": "E1", "kind": "fire", "ref_id": "fire:v1", "rule": "firms_hotspot",
        "distance_km": 0.8, "dt_minutes": 60, "score": 0.6,
        "details": {"frp_mw": 40.0, "lat": 32.115, "lon": 20.07, "satellite": "N20",
                    "ts": iso(T0 + timedelta(hours=1))}}],
        summaries=[{"event_id": "E1", "score": 0.6, "kinds": ["fire"], "evidence": 1,
                    "corroborated": True}])
    sstore = StorylineStore(tmp_path / "storylines.sqlite")
    r = SimpleNamespace(store=store, lake=FakeLake(), storyline_store=sstore,
                        ingestor=SimpleNamespace(live={}, _lock=threading.Lock()),
                        settings=SimpleNamespace(data_dir=tmp_path, config_dir=tmp_path,
                                                 api_token=""))
    r.job = StorylineJob(store, sstore, StorylineConfig(), clock=lambda: NOW,
                         country_names={"LY": "Libya", "FI": "Finland"})
    yield r
    sstore.close()
    store.close()


@pytest.fixture
def client(rt):
    app = FastAPI()
    mount_storylines(app, rt)
    mount_intel(app, rt)
    return TestClient(app)


def _bz(rt):
    return next(s for s in rt.storyline_store.list(min_events=1) if "Merkury" in s["title"])


def test_full_pass_stores_threads_with_event_time_span(rt):
    out = rt.job.run_once()
    assert out["mode"] == "full" and out["storylines"] == 2 and out["written"] == 2
    bz = rt.storyline_store.get(_bz(rt)["id"])
    assert bz["id"] == "SLE1" and bz["theatre_label"] == "Libya · conflict"
    assert [m["event_id"] for m in bz["members"]] == ["E1", "E2", "E3", "E4"]
    origin = bz["members"][0]
    # the stated "late on November 21" (local, by longitude) moves the origin before its
    # first report, and says so
    assert origin["t_basis"] == "occurred_claims" and origin["t_precision"] == "part_of_day"
    assert bz["t_first"] == origin["t"] < iso(T0) and bz["t_first_basis"] == "occurred_claims"
    assert bz["t_last"] >= bz["members"][-1]["t"]
    assert bz["roles"] == {"origin": 1, "development": 1, "follow_up": 1, "reaction": 1}
    assert bz["links"] and all(lk["reasons"] for lk in bz["links"])
    assert rt.job.run_once()["mode"] == "skipped"  # nothing changed: one cheap query


def test_incremental_pass_keeps_ids_and_full_pass_redirects(rt):
    rt.job.run_once()
    late = node("E5", 40, "Second sea drone attack on Russian ship Ascalon off Benghazi",
                category="maritime_security", lat=32.2, lon=20.0, place="Benghazi",
                country="LY", angle=28, mentioned=("p:benghazi",))
    rt.store.save_events([as_row(late, now=NOW + timedelta(minutes=5))])
    out = rt.job.run_once(NOW + timedelta(minutes=6))
    assert out["mode"] == "incremental" and out["written"] == 1 and out["unchanged"] == 1
    bz = rt.storyline_store.get("SLE1")
    assert "E5" in {m["event_id"] for m in bz["members"]}
    # E1 merged into E2 (event consolidation): a full pass re-derives; the thread keeps
    # its id because it still holds most of its members
    rt.store.merge_events([{"survivor": "E2", "loser": "E1", "similarity": 0.9,
                            "fields": []}], [])
    out = rt.job.run_once(NOW + timedelta(minutes=8))
    assert out["mode"] == "full"
    assert rt.storyline_store.get("SLE1")["origin_event_id"] == "E2"


def test_assign_ids_reuse_and_redirect():
    recs = [{"n_events": 3, "t_first": "a", "origin_event_id": "X1",
             "members": [{"event_id": e} for e in ("X1", "X2", "Y1")]}]
    out, redirects = assign_ids(recs, {"SLX1": {"X1", "X2"}, "SLY1": {"Y1", "Y2"}})
    assert out[0]["id"] == "SLX1" and redirects == {"SLY1": "SLX1"}


def test_list_filters_and_detail_api(rt, client):
    rt.job.run_once()
    j = client.get("/api/storylines").json()
    assert [s["id"] for s in j["items"]][:1] and {s["theatre_label"] for s in j["items"]} == {
        "Libya · conflict", "Finland · conflict"}
    assert j["items"] == sorted(j["items"], key=lambda s: s["t_last"], reverse=True)
    assert {t["label"] for t in j["theatres"]} == {"Libya · conflict", "Finland · conflict"}
    # bbox around Benghazi, category and free text
    j = client.get("/api/storylines?bbox=19,31,21,33").json()
    assert [s["id"] for s in j["items"]] == ["SLE1"]
    j = client.get("/api/storylines?category=political").json()
    assert [s["id"] for s in j["items"]] == ["SLE1"]
    assert client.get("/api/storylines?q=Sparta").json()["items"][0]["origin_event_id"] == "F1"
    assert client.get(f"/api/storylines?until={iso(T0 - timedelta(days=3))}").json()[
        "items"] == []
    d = client.get("/api/storylines/SLE1").json()
    assert d["members"][0]["role"] == "origin" and d["links"][0]["reasons"]
    assert client.get("/api/storylines/nope").status_code == 404
    assert client.get("/api/storylines?since=garbage").status_code == 422
    m = client.get("/api/events/E4/storyline").json()["storyline"]
    assert m["storyline_id"] == "SLE1" and m["role"] == "reaction"
    assert client.get("/api/events/F9/storyline").json()["storyline"] is None


def test_storyline_timeline_merges_member_timelines(rt, client):
    rt.job.run_once()
    tl = client.get("/api/storylines/SLE1/timeline").json()
    assert tl["storyline_id"] == "SLE1" and set(tl["lanes"]) >= {"reports", "sensors"}
    ents = tl["entries"]
    assert {e["event_id"] for e in ents} == {"E1", "E2", "E3", "E4"}
    assert all(e["lane"] and e["event_role"] for e in ents)
    first = [e for e in ents if "first_report" in e["flags"]]
    assert len(first) == 1 and first[0]["event_id"] == "E1" and first[0]["t_rel_min"] == 0
    fire = next(e for e in ents if e.get("sensor") == "fire")
    assert fire["lane"] == "sensors" and "first_sensor" in fire["flags"]
    occ = {o["event_id"]: o for o in tl["occurred_by_event"]}
    assert occ["E1"]["basis"] == "claims" and occ["E2"]["basis"] == "before_first_report"
    assert tl["occurred"]["basis"] == "claims"  # the head: the origin's occurred band
    assert ents == sorted(ents, key=lambda e: (e.get("t") or e.get("lo")))


def test_intel_storyline_section(rt, client):
    rt.job.run_once()
    s = client.get("/api/intel?kind=event&id=E3&sections=storyline").json()["sections"]
    sl = s["storyline"]
    assert sl["member"]["storyline_id"] == "SLE1" and sl["member"]["role"] == "follow_up"
    assert [m["event_id"] for m in sl["members"]] == ["E1", "E2", "E3", "E4"]
    s = client.get("/api/intel?kind=place&lat=32.1&lon=20.1&label=Benghazi"
                   "&sections=storyline").json()["sections"]
    assert [x["id"] for x in s["storyline"]["nearby"]] == ["SLE1"]
