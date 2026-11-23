"""TerrorJob end to end with fakes (event store, extraction store, LLM backend, HTTP via
respx) and the read-only API on the resulting store."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.terror import mount_terror
from osint.llm.base import LLMResult
from osint.pipeline.extract.config import FetchConfig
from osint.terror.config import TerrorConfig
from osint.terror.job import TerrorJob
from osint.terror.sources import designations as des
from osint.terror.sources import official
from osint.terror.store import TerrorStore
from tests.terror.conftest import FIX, ROOT, doc, event, geocoder, terr

NOW = datetime(2026, 11, 23, 12, tzinfo=UTC)
GEO = geocoder({"Peshawar": (34.008, 71.578, "PK", "city")})


class FakeEvents:
    def __init__(self, events):
        self.events = {e["id"]: e for e in events}

    def recent_events(self, since, origins=("news",)):
        return [dict(e, cluster_state={}) for e in self.events.values()]

    def event_documents_for(self, ids):
        return {i: self.events[i]["documents"] for i in ids if i in self.events}

    def get_event(self, eid, follow=True):
        return self.events.get(eid)

    def resolve_event_id(self, eid):
        return eid


class FakeBase:
    def __init__(self, recs=None):
        self.recs = recs or {}

    def get_many(self, ids):
        return {i: self.recs[i] for i in ids if i in self.recs}


class FakeBackend:
    name = "fake"

    def __init__(self, data):
        self.data, self.calls = data, 0

    async def extract(self, instruction, text, schema):
        self.calls += 1
        return LLMResult(data=self.data(text) if callable(self.data) else self.data,
                         cost_usd=0.002, model="fake-haiku", latency_s=0.1)


def _events():
    bomb = event("E1", "Suicide bomber kills 12 at market in Peshawar", docs=[
        doc("https://www.dawn.com/1", "Suicide bomber kills 12 at market in Peshawar"),
        doc("https://www.reuters.com/2", "Blast at crowded Pakistan bazaar leaves 12 dead")])
    war = event("E2", "Russian forces attack Odesa region, civilian infrastructure damaged",
                docs=[doc("https://www.ukrinform.net/3", "Russian forces attack Odesa region")])
    denial = event("E3", "Contrary to reports, there are no missile launches identified",
                   docs=[doc("https://t.me/x/1", "Contrary to reports, there are no missile "
                                                 "launches identified", source="telegram:x")])
    other = event("E4", "Senegal budget defeat puts IMF programme at risk",
                  docs=[doc("https://t.me/y/2", "Senegal budget", source="telegram:y")])
    return [bomb, war, denial, other]


def _job(tmp_path, backend=None, base=None):
    from osint.terror.groups import GroupKB

    store = TerrorStore(tmp_path / "terror.sqlite")
    cfg = TerrorConfig()
    cfg.sources.timeout_s = 5
    kb = GroupKB.load(ROOT / "config" / "terror_groups.yaml")
    return TerrorJob(FakeEvents(_events()), store, cfg, kb, base_store=base or FakeBase(),
                     backend=backend, geocoder=GEO, fetch_cfg=FetchConfig(min_chars=10),
                     clock=lambda: NOW.timestamp())


async def test_llm_only_for_candidates_not_excluded_by_rule(tmp_path):
    backend = FakeBackend(terr())
    job = _job(tmp_path, backend)
    with respx.mock:
        respx.get("https://www.dawn.com/1").mock(return_value=httpx.Response(
            200, text="<html><body><p>" + "A suicide bomber killed 12 people at a market in "
                      "Peshawar. Tehrik-i-Taliban Pakistan claimed responsibility. " * 5
            + "</p></body></html>", headers={"content-type": "text/html"}))
        cands = job.candidates(NOW)
        items = job.select_llm(cands, NOW)
        stats = await job.extract_pass(items, NOW)
    # E2 is state warfare (rule), E3 a denial (rule), E4 not a candidate: one call only
    assert [i.event["id"] for i in items] == ["E1"] and stats["ok"] == 1 and backend.calls == 1
    assert job.store.cost_since(0)["cost_usd"] == 0.002
    out = job.derive(NOW)
    assert out["counts"].get("attack:confirmed") == 1
    rec = job.store.incident("ti-E1")
    assert rec["group"]["group_id"] == "ttp" and rec["group"]["kind"] == "link"
    assert rec["attribution"]["level"] == "claimed"
    assert job.store.incident("ti-E3") is None  # a denial is never an incident
    assert job.store.incident("ti-E4") is None
    # incidents for events that stop being candidates are removed, others are kept
    job.events.events["E1"]["title"] = "Budget talks"
    job.events.events["E1"]["documents"] = [doc("https://www.dawn.com/1", "Budget talks")]
    job.derive(NOW)
    assert job.store.incident("ti-E1") is None


@respx.mock
async def test_sources_refresh_levels_and_designations(tmp_path):
    job = _job(tmp_path)
    respx.get(official.MI5_URL).mock(return_value=httpx.Response(200, content=(
        FIX / "mi5.xml").read_bytes()))
    respx.get(des.FTO_URL).mock(return_value=httpx.Response(200, content=(
        FIX / "fto.html").read_bytes()))
    res = await job.refresh_sources(NOW, only=["mi5_threat_level", "us_fto"])
    assert res == {"mi5_threat_level": "ok (2)", "us_fto": "ok (9)"}
    lv = job.store.levels("GB")
    assert {x["level"] for x in lv} == {"SEVERE", "SUBSTANTIAL"}
    fto = job.store.designations(list_id="us_fto")
    isisk = next(d for d in fto if "ISIS-K" in d["name"])
    assert isisk["group_id"] == "iskp" and "exact name match" in isisk["group_basis"]
    assert job.store.designation_changes() == []  # first load is the baseline, not "new"
    # a later fetch where one group disappears and one is added -> two changes
    page = (FIX / "fto.html").read_text(encoding="utf-8").replace("Los Viagras", "Los Nuevos")
    respx.get(des.FTO_URL).mock(return_value=httpx.Response(200, text=page))
    job.store.record_source("us_fto", now=NOW, ok=True, content_hash="old")
    await job.refresh_sources(NOW, only=["us_fto"])
    ch = {(c["change"], c["name"]) for c in job.store.designation_changes()}
    assert ("added", "Los Nuevos") in ch and ("removed", "Los Viagras") in ch


@respx.mock
async def test_level_change_is_recorded(tmp_path):
    job = _job(tmp_path)
    route = respx.get(official.MI5_URL)
    route.mock(return_value=httpx.Response(200, content=(FIX / "mi5.xml").read_bytes()))
    await job.refresh_sources(NOW, only=["mi5_threat_level"])
    changed = (FIX / "mi5.xml").read_text(encoding="utf-8").replace("SEVERE", "SUBSTANTIAL")
    route.mock(return_value=httpx.Response(200, text=changed))
    await job.refresh_sources(NOW, only=["mi5_threat_level"])
    hist = job.store.level_history("GB")
    assert hist and hist[0]["prev_level"] == "SEVERE" and hist[0]["level"] == "SUBSTANTIAL"


@respx.mock
async def test_failed_source_is_recorded_and_others_continue(tmp_path):
    job = _job(tmp_path)
    respx.get(official.MI5_URL).mock(return_value=httpx.Response(503))
    respx.get(official.ASIO_URL).mock(return_value=httpx.Response(200, content=(
        FIX / "asio.html").read_bytes()))
    res = await job.refresh_sources(NOW, only=["mi5_threat_level", "asio_threat_level"])
    assert res["mi5_threat_level"].startswith("error") and res["asio_threat_level"] == "ok (1)"
    st = {s["source_id"]: s for s in job.store.sources()}
    assert st["mi5_threat_level"]["ok"] == 0 and st["mi5_threat_level"]["http_status"] == 503


async def test_api_endpoints(tmp_path):
    job = _job(tmp_path, FakeBackend(terr()))
    job.store.save_extraction("E1", now=NOW, status="done", result=terr())
    job.derive(NOW)
    job.store.save_level({"country": "GB", "authority": "MI5", "scope": "national",
                          "level": "SEVERE", "rank": 4, "scale_max": 5, "as_of": "2026-04-30",
                          "quote": "q", "source_url": "https://x"}, NOW)
    job.compute_indicators(NOW)
    rt = SimpleNamespace(terror_store=job.store, settings=SimpleNamespace(
        data_dir=tmp_path, config_dir=ROOT / "config"))
    app = FastAPI()
    mount_terror(app, rt)
    c = TestClient(app)
    d = c.get("/api/terror/definition").json()
    assert d["criteria"]["required"][2]["id"] == "sub_national"
    inc = c.get("/api/terror/incidents?days=3650").json()
    assert inc["count"] == 1 and inc["items"][0]["status"] == "confirmed"
    assert "event time" in inc["order"]
    full = c.get("/api/terror/incidents/ti-E1").json()
    assert full["outlets"]["n"] == 2 and full["extraction_meta"]["model"] is None
    assert c.get("/api/terror/incidents/nope").status_code == 404
    gb = c.get("/api/terror/board/GB").json()
    assert gb["official_levels"][0]["level"] == "SEVERE" and "posture" in gb
    assert c.get("/api/terror/board/ZZ").status_code == 404
    glob = c.get("/api/terror/board/global").json()
    assert glob["scope"] == "global" and "hotspots_ucdp" in glob
    groups = c.get("/api/terror/groups?q=taliban").json()
    assert any(g["id"] == "ttp" for g in groups["items"])
    page = c.get("/api/terror/groups/ttp").json()
    assert page["incidents"] and page["incidents"][0]["event_id"] == "E1"
    feed = c.get("/api/terror/feed?days=400").json()
    assert any(x["type"] == "incident" for x in feed["items"])
    src = c.get("/api/terror/sources").json()["sources"]
    assert any(s["id"] == "gtd" and s["enabled"] is False for s in src)
    assert c.get("/api/terror/eval").json()["available"] is False


def test_licences_register_includes_terror_sources():
    from osint.api.licences import licences

    ids = {s["id"] for s in licences()["sources"]}
    assert {"terror:us_fto", "terror:ucdp_osv", "terror:gtd"} <= ids


def test_intel_section_registered():
    from osint.intel import aggregate

    assert "terror" in aggregate.SECTIONS and "terror" in aggregate.SIMPLE


def test_eval_labels_fixture_is_well_formed():
    data = json.loads((FIX / "eval_labels.json").read_text(encoding="utf-8"))
    items = data["items"]
    assert len(items) >= 150
    allowed = {"attack", "possible", "plot", "arrest", "prosecution", "threat", "ct_operation",
               "no"}
    assert all(set(i["label"]) <= allowed and i["label"] for i in items)
    assert len({i["id"] for i in items}) == len(items)
