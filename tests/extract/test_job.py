import asyncio
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.extraction import mount_extraction
from osint.llm import CallRateLimiter, LLMError, LLMResult
from osint.pipeline.extract.config import ExtractionConfig, FetchConfig
from osint.pipeline.extract.job import ExtractionJob, best_document, get_store
from osint.pipeline.extract.store import ExtractionStore

ARTICLE = ("SECRET-ARTICLE-BODY " + "Officials said a missile hit Kharkiv on Monday, killing "
           "two people and damaging a power station. ") * 5
HTML = f"<html><body><article><p>{ARTICLE}</p></article></body></html>"


def ev(eid, *, div=1, conf=0.5, upd="2026-11-21T10:00:00.000+00:00", version=1, **kw):
    return {"id": eid, "category": "armed_conflict", "title": f"title {eid}", "country": "UA",
            "precision": "city", "lat": 49.98, "lon": 36.25, "place_name": "Kharkiv",
            "status": "reported", "confidence": conf, "source_diversity": div,
            "updated_at": upd, "version": version, **kw}


def doc(did, role="primary", url=None, pub="2026-11-21T09:00:00.000+00:00"):
    return {"doc_id": did, "url": url or f"https://news.test/{did}", "title": f"Headline {did}",
            "role": role, "source": "rss", "published_at": pub, "language": "en"}


class FakeEvents:
    def __init__(self, events, docs):
        self.events, self.docs = events, docs
        self.list_kwargs = None

    def list_events(self, **kw):
        self.list_kwargs = kw
        return [dict(e) for e in self.events]

    def get_event(self, eid):
        e = next((x for x in self.events if x["id"] == eid), None)
        return None if e is None else {**e, "documents": self.docs.get(eid, [])}


class FakeBackend:
    name = "fake"

    def __init__(self, data=None, fail=False):
        self.calls: list[tuple[str, str]] = []
        self.data = data or {
            "is_real_event": True, "event_type": "armed_conflict", "subtype": "missile strike",
            "summary": "Missile hit Kharkiv.", "when": None,
            "where": {"place_name": "Kharkiv", "admin1": None, "country": "UA",
                      "precision": "city"},
            "actors": [], "casualties": {"killed": 2, "injured": None},
            "weapons_platforms": [], "cause_stated": None, "consequences": [],
            "claim_type": "official",
            "quotes": [{"field": "casualties", "quote": "killing two people"}]}
        self.fail = fail

    async def extract(self, instruction, text, schema):
        self.calls.append((instruction, text))
        if self.fail:
            raise LLMError("boom")
        await asyncio.sleep(0)
        return LLMResult(self.data, cost_usd=0.002, model="haiku-x", latency_s=1.5)


def make_job(tmp_path, events, docs, backend=None, *, clock=None, **cfg):
    cfg = ExtractionConfig(fetch=FetchConfig(min_chars=50), **cfg)
    store = ExtractionStore(tmp_path / "extraction.sqlite")
    clock = clock or (lambda: 1_800_000_000.0)

    def handler(req):
        return httpx.Response(200, html=HTML) if "bad" not in req.url.path \
            else httpx.Response(403)

    job = ExtractionJob(
        FakeEvents(events, docs), store, cfg, backend or FakeBackend(), clock=clock,
        limiter=CallRateLimiter(cfg.max_calls_per_hour, cfg.max_calls_per_day, clock=clock),
        geocoder=lambda p, a, c: (49.98, 36.25, "UA", "city"),
        http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return job, store


def test_best_document_prefers_primary_http():
    docs = [doc("g", "gdelt", pub="2026-01-01"), doc("m", "mention", pub="2026-01-02"),
            doc("p2", pub="2026-01-05"), doc("p1", pub="2026-01-03"),
            doc("x", url="javascript:alert(1)", pub="2000")]
    assert best_document(docs)["doc_id"] == "p1"
    assert best_document([doc("x", url="ftp://x")]) is None


async def test_priority_order_and_cap_per_run(tmp_path):
    events = [ev("A", div=1, conf=0.99), ev("B", div=2, conf=0.6),
              ev("C", div=3, conf=0.9), ev("D", div=2, conf=0.6, upd="2026-11-21T11:00:00Z"),
              ev("S", div=2, conf=1.0)]  # S: sensor event without documents
    docs = {e: [doc(f"d{e}")] for e in "ABCD"}
    job, _ = make_job(tmp_path, events, docs, max_per_run=3)
    items = job.select()
    assert [i.event["id"] for i in items] == ["C", "D", "B"]
    assert job.queue_size == 5


async def test_run_extracts_and_is_idempotent(tmp_path):
    backend = FakeBackend()
    events = [ev("A", div=2)]
    docs = {"A": [doc("d1")]}
    job, store = make_job(tmp_path, events, docs, backend)
    res = await job.run_once()
    assert len(res) == 1 and res[0]["verdict"] == "supported"
    rec = store.get("A")
    assert rec["status"] == "done" and rec["verdict"] == "supported" and rec["doc_id"] == "d1"
    assert rec["text_source"] == "direct" and rec["cost_usd"] == 0.002
    assert rec["quotes"][0]["verbatim"] is True and rec["model"] == "haiku-x"
    assert "SECRET-ARTICLE-BODY" in backend.calls[0][1]
    # second pass: nothing to do
    assert await job.run_once() == [] and len(backend.calls) == 1
    # event version bumped, same primary doc: no new call, version remembered
    events[0]["version"] = 2
    await job.run_once()
    assert len(backend.calls) == 1 and store.get("A")["event_version"] == 2
    assert store.get("A")["cost_usd"] == 0.002
    # primary doc changed: re-extract
    events[0]["version"] = 3
    docs["A"].insert(0, doc("d0", pub="2026-11-21T08:00:00.000+00:00"))
    await job.run_once()
    assert len(backend.calls) == 2 and store.get("A")["doc_id"] == "d0"
    store.close()
    # article text never persisted
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("extraction.sqlite*"))
    assert b"SECRET-ARTICLE-BODY" not in raw


async def test_snippet_fallback_and_verdicts(tmp_path):
    not_real = FakeBackend(data={**FakeBackend().data, "is_real_event": False})
    job, store = make_job(tmp_path, [ev("A")], {"A": [doc("bad1")]}, not_real)
    job.snippet_lookup = lambda d: "snippet text"
    await job.run_once()
    rec = store.get("A")
    assert rec["verdict"] == "unsupported" and rec["text_source"] == "snippet"
    assert "snippet text" in not_real.calls[0][1] and "403" in rec["result"]["fetch_error"]


async def test_errors_are_retried_later_with_attempt_limit(tmp_path):
    t = {"now": 1_800_000_000.0}
    backend = FakeBackend(fail=True)
    job, store = make_job(tmp_path, [ev("A")], {"A": [doc("d1")]}, backend,
                          clock=lambda: t["now"], retry_after_min=0, max_attempts=2)
    await job.run_once()
    assert store.get("A")["status"] == "error" and store.get("A")["attempts"] == 1
    assert job.status()["last_errors"][0]["error"].startswith("LLMError")
    await job.run_once()
    assert store.get("A")["attempts"] == 2
    await job.run_once()  # max attempts reached
    assert len(backend.calls) == 2
    assert store.last_errors()[0]["event_id"] == "A"


async def test_rate_caps_stop_calls(tmp_path):
    events = [ev(c, div=2) for c in "ABCDE"]
    backend = FakeBackend()
    job, _ = make_job(tmp_path, events, {c: [doc(c)] for c in "ABCDE"}, backend,
                      max_calls_per_hour=2, max_calls_per_day=10)
    await job.run_once()
    assert len(backend.calls) == 2 and job.capped
    assert await job.run_once() == [] and len(backend.calls) == 2
    assert job.status()["calls"]["hour"] == 2


async def test_concurrency_limit(tmp_path):
    state = {"now": 0, "peak": 0}

    class Slow(FakeBackend):
        async def extract(self, instruction, text, schema):
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(0.02)
            state["now"] -= 1
            return await super().extract(instruction, text, schema)

    events = [ev(c) for c in "ABCDEF"]
    job, _ = make_job(tmp_path, events, {c: [doc(c)] for c in "ABCDEF"}, Slow(),
                      concurrency=2)
    assert len(await job.run_once()) == 6 and state["peak"] == 2


def test_api_status_and_event(tmp_path):
    rt = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path), booting=True)
    app = FastAPI()
    mount_extraction(app, rt)
    client = TestClient(app)
    body = client.get("/api/extraction/status").json()
    assert body["running"] is False and body["booting"] is True
    assert client.get("/api/extraction/E1").status_code == 404
    assert client.get("/api/extraction/..%2Fx").status_code == 404

    store = get_store(rt)
    job, _ = make_job(tmp_path / "j", [], {})
    job.store = store
    rt.extraction_job = job
    store.save("E1", doc_id="d", url="https://x", status="done", now=__import__(
        "datetime").datetime(2026, 11, 21, tzinfo=__import__("datetime").UTC),
        is_real_event=True, verdict="supported", result={"summary": "s", "quotes": [
            {"field": "where", "quote": "q", "verbatim": True}]}, cost_usd=0.01)
    store.log_call(job.clock(), event_id="E1", ok=True, cost_usd=0.01)
    status = client.get("/api/extraction/status").json()
    assert status["running"] is True and status["calls"]["day"] == 0  # limiter is the job's
    assert status["cost_usd"]["total"] == 0.01 and status["verdicts"] == {"supported": 1}
    for key in ("queue_size", "last_errors", "backend", "model"):
        assert key in status
    got = client.get("/api/extraction/E1").json()
    assert got["verdict"] == "supported" and got["quotes"][0]["quote"] == "q"


def test_config_loads_repo_yaml():
    from osint.config import ROOT
    cfg = ExtractionConfig.load(ROOT / "config" / "extraction.yaml")
    assert cfg.backend == "claude_cli" and cfg.model == "haiku" and cfg.concurrency == 2
    assert (cfg.max_calls_per_hour, cfg.max_calls_per_day) == (120, 1500)
    assert cfg.min_sources == 1 and cfg.fetch.max_chars == 12000 and cfg.fetch.timeout_s == 15
    assert ExtractionConfig.load(ROOT / "nope.yaml").interval_s == 120
