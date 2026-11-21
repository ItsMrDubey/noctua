import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osint.api.events import mount_events
from osint.llm import CallRateLimiter, LLMError, LLMResult
from osint.store.sqlite import SqliteStore, iso
from osint.translate.batch import BatchTranslator, parse_items
from osint.translate.config import TranslateConfig
from osint.translate.english import annotate_events
from osint.translate.job import TranslateJob, is_english
from osint.translate.store import TranslationStore, clean, text_key

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


class FakeBackend:
    """Echoes '<EN> text' per item; ``drop`` indices are left out of the first answer(s)."""

    name = "fake"
    model = "fake-haiku"

    def __init__(self, drop_first=(), drop_always=(), fail=False):
        self.calls: list[list[dict]] = []
        self.drop_first, self.drop_always, self.fail = set(drop_first), set(drop_always), fail

    async def extract(self, instruction, text, schema):
        items = json.loads(text)
        self.calls.append(items)
        if self.fail:
            raise LLMError("boom")
        out = []
        for it in items:
            src = it["text"]
            if src in self.drop_always or (len(self.calls) == 1 and src in self.drop_first):
                continue
            out.append({"i": it["i"], "en": f"<EN> {src}"})
        return LLMResult(data={"items": out}, cost_usd=0.01, model="fake-haiku",
                         latency_s=1.5)


def _event(i, *, title, summary="", status="reported", precision="city", conf=0.6,
           updated=T0):
    return {"id": f"01EV{i:022d}", "category": "armed_conflict", "title": title,
            "summary": summary, "actors": [], "lat": 50.0, "lon": 36.0,
            "precision": precision, "place_name": "P", "country": "UA",
            "geo_confidence": 0.9, "h3_3": "831196fffffffff",
            "t_start": iso(T0 - timedelta(hours=1)), "t_end": iso(updated), "status": status,
            "confidence": conf, "version": 1, "mention_count": 1, "source_diversity": 1,
            "gdelt_ids": [], "first_seen": iso(T0), "updated_at": iso(updated),
            "cluster_state": {}}


def _doc(i, doc, *, title, lang, role="primary"):
    return {"event_id": f"01EV{i:022d}", "doc_id": doc, "source": "rss:x",
            "url": f"https://x.example/{doc}", "title": title, "published_at": iso(T0),
            "language": lang, "role": role, "added_at": iso(T0)}


@pytest.fixture
def events(tmp_path):
    s = SqliteStore(tmp_path / "e.sqlite")
    s.save_events(
        [_event(1, title="Удар по Харкову", summary="Росія вдарила по Харкову вночі",
                updated=T0 - timedelta(hours=3)),
         _event(2, title="Russia strikes Kharkiv", summary="The strike hit the city"),
         _event(3, title="حمله به تهران", precision="country", conf=0.2),  # not map-visible
         _event(4, title="Massaker 1994", status="historical")],
        documents=[_doc(1, "d1", title="Удар по Харкову", lang="uk"),
                   _doc(1, "d1b", title="Обстріл Харкова", lang="uk", role="mention"),
                   _doc(2, "d2", title="Russia strikes Kharkiv", lang="en"),
                   _doc(3, "d3", title="حمله به تهران", lang="fa")])
    yield s
    s.close()


@pytest.fixture
def tstore(tmp_path):
    s = TranslationStore(tmp_path / "t.sqlite")
    yield s
    s.close()


def _job(events, tstore, backend, **cfg):
    snips = {"d1": "Сніпет один", "d2": "English snippet here", "d3": "اسنیپت"}
    return TranslateJob(events, tstore, TranslateConfig(**cfg), backend,
                        snippets=lambda docs: {d["doc_id"]: snips[d["doc_id"]] for d in docs
                                               if d["doc_id"] in snips},
                        clock=lambda: T0.timestamp())


# ------------------------------------------------------------------ batch translator


def test_parse_items_validates_indices():
    data = {"items": [{"i": 0, "en": "a"}, {"i": 0, "en": "dup"}, {"i": 5, "en": "x"},
                      {"i": -1, "en": "x"}, {"i": True, "en": "x"}, {"i": 2, "en": "  "},
                      {"i": 1, "en": " b  c "}, "junk"]}
    assert parse_items(data, 3) == {0: "a", 1: "b c"}
    assert parse_items({"nope": 1}, 3) == {}


async def test_batch_maps_back_by_index():
    be = FakeBackend()
    items = [{"key": f"k{n}", "src_lang": "fr", "text": f"t{n}"} for n in range(5)]
    out = await BatchTranslator(be).translate(items)
    assert out.done == {f"k{n}": f"<EN> t{n}" for n in range(5)}
    assert len(be.calls) == 1 and [c["i"] for c in be.calls[0]] == list(range(5))
    assert be.calls[0][0]["lang"] == "fr"


async def test_partial_failure_retried_once_then_failed():
    be = FakeBackend(drop_first={"t1", "t3"}, drop_always={"t3"})
    items = [{"key": f"k{n}", "src_lang": "fr", "text": f"t{n}"} for n in range(4)]
    out = await BatchTranslator(be).translate(items)
    assert len(be.calls) == 2 and [c["text"] for c in be.calls[1]] == ["t1", "t3"]
    assert [c["i"] for c in be.calls[1]] == [0, 1]  # re-indexed for the retry
    assert set(out.done) == {"k0", "k1", "k2"} and out.done["k1"] == "<EN> t1"
    assert set(out.failed) == {"k3"}


async def test_backend_error_fails_batch_after_retry():
    be = FakeBackend(fail=True)
    out = await BatchTranslator(be).translate([{"key": "k", "src_lang": "fr", "text": "t"}])
    assert len(be.calls) == 2 and out.failed == {"k": "LLMError: boom"} and not out.done


async def test_capped_leaves_items_pending():
    out = await BatchTranslator(FakeBackend(), acquire=lambda: False).translate(
        [{"key": "k", "src_lang": "fr", "text": "t"}])
    assert not out.done and not out.failed and not out.calls


# ------------------------------------------------------------------ cache


def test_english_passthrough_and_cache_keys(tstore):
    assert is_english("Hello", "en") and is_english("The strike hit the city of Kyiv", "")
    assert not is_english("Удар по Харкову", "uk")
    assert not is_english("Raketeneinschlag: NATO-Generalsekretär sagt Polen Unterstützung zu",
                          "de")  # the tag wins over the stopword heuristic
    assert text_key("uk", "x") != text_key("ru", "x") and text_key("EN-us", "x") == \
        text_key("en", "x")
    assert clean("  a \n b ", 300) == "a b" and len(clean("x" * 500, 300)) == 300
    items = [{"key": text_key("en", "Hi there"), "lang": "en", "text": "Hi there",
              "english": True},
             {"key": text_key("uk", "Привіт"), "lang": "uk", "text": "Привіт"}]
    assert tstore.enqueue(items, T0) == 2
    assert tstore.enqueue(items, T0) == 0  # cache hit: nothing new
    row = tstore.get(text_key("en", "Hi there"))
    assert row["status"] == "passthrough" and row["text_en"] == "Hi there"
    due = tstore.due(10, T0, timedelta(minutes=30), 3)
    assert [d["text"] for d in due] == ["Привіт"]
    # a row once passed through but no longer judged English is requeued
    tstore.enqueue([{**items[0], "english": False}], T0)
    row = tstore.get(text_key("en", "Hi there"))
    assert row["status"] == "pending" and row["text_en"] is None


async def test_failed_items_retried_after_delay(tstore):
    k = text_key("uk", "Привіт")
    tstore.enqueue([{"key": k, "lang": "uk", "text": "Привіт"}], T0)
    tstore.save_failed([k], "nope", T0)
    assert tstore.due(10, T0 + timedelta(minutes=5), timedelta(minutes=30), 3) == []
    assert len(tstore.due(10, T0 + timedelta(minutes=31), timedelta(minutes=30), 3)) == 1
    tstore.save_failed([k], "nope", T0)
    tstore.save_failed([k], "nope", T0)  # 3 attempts: given up
    assert tstore.due(10, T0 + timedelta(days=1), timedelta(minutes=30), 3) == []


# ------------------------------------------------------------------ job


async def test_job_translates_and_prioritises(events, tstore):
    be = FakeBackend()
    job = _job(events, tstore, be, batch_size=3)
    res = await job.run_once(max_calls=1)
    assert res["calls"] == 1 and res["done"] == 3 and res["queue"] > 0
    first = [c["text"] for c in be.calls[0]]
    # map-visible event 1 (uk) first: its title and summary, then its documents
    assert first[:2] == ["Удар по Харкову", "Росія вдарила по Харкову вночі"]
    assert "حمله به تهران" not in first  # event 3 is not map-visible: later
    texts = {r["text"] for r in tstore.get_many(
        [text_key("en", "Russia strikes Kharkiv")]).values()}
    assert texts == {"Russia strikes Kharkiv"}  # English: passthrough, never sent
    assert all("Massaker" not in c["text"] for call in be.calls for c in call)  # historical
    await job.run_once(max_calls=10)
    assert job.queue_size == 0
    sent = [c["text"] for call in be.calls for c in call]
    assert len(sent) == len(set(sent))  # each text translated once (event title == doc title)
    assert "English snippet here" not in sent and "Сніпет один" in sent
    assert "Massaker 1994" not in sent
    st = job.status()
    assert st["calls"]["today_utc"]["calls"] == len(be.calls)
    assert st["queue"] == 0 and st["failures"]["count"] == 0


async def test_job_respects_caps_and_shared_limiter(events, tstore):
    be = FakeBackend()
    shared = CallRateLimiter(1, 100, clock=lambda: T0.timestamp(),
                             history=[T0.timestamp() - 10])
    job = _job(events, tstore, be)
    job.shared_limiter = shared
    res = await job.run_once()
    assert res["calls"] == 0 and res["capped"] and not be.calls and res["queue"] > 0


async def test_incremental_scan_after_backfill(events, tstore):
    be = FakeBackend()
    job = _job(events, tstore, be)
    await job.run_once()
    assert job._since is not None
    n = len(be.calls)
    await job.run_once()
    assert len(be.calls) == n  # nothing new to translate


# ------------------------------------------------------------------ API fields


class _RT:
    def __init__(self, store, tstore):
        self.store = store
        self.translation_store = tstore


async def test_api_english_fields(events, tstore):
    job = _job(events, tstore, FakeBackend(drop_always={"Обстріл Харкова"}))
    app = FastAPI()
    rt = _RT(events, tstore)
    mount_events(app, rt)
    client = TestClient(app)
    ev1, ev2, ev3 = (f"01EV{i:022d}" for i in (1, 2, 3))

    items = {it["id"]: it for it in client.get("/api/events?all=true").json()["items"]}
    assert items[ev1]["title_en"] is None and items[ev1]["title"] == "Удар по Харкову"
    assert items[ev2]["title_en"] == "Russia strikes Kharkiv"  # English needs no cache

    await job.run_once(max_calls=10)
    items = {it["id"]: it for it in client.get("/api/events?all=true").json()["items"]}
    assert items[ev1]["title_en"] == "<EN> Удар по Харкову" and items[ev1]["lang"] == "uk"
    assert items[ev2]["lang"] == "en" and items[ev3]["lang"] == "fa"

    d = client.get(f"/api/events/{ev1}").json()
    assert d["title"] == "Удар по Харкову" and d["summary_en"] == \
        "<EN> Росія вдарила по Харкову вночі"
    docs = {x["doc_id"]: x for x in d["documents"]}
    assert docs["d1"]["title_en"] == "<EN> Удар по Харкову" and docs["d1"]["lang"] == "uk"
    assert docs["d1"]["snippet_en"] == "<EN> Сніпет один"
    assert docs["d1b"]["title_en"] is None  # failed: UI shows the original
    d2 = client.get(f"/api/events/{ev2}").json()
    assert d2["documents"][0]["snippet_en"] == "English snippet here"

    kml = client.get("/kml/events.kml").text
    assert "&lt;EN&gt; Удар по Харкову" in kml

    st = client.get("/api/translate/status").json()
    assert st["running"] is False and st["texts"]["failed"] == 1
    assert st["calls"]["total"]["calls"] >= 1


def test_supported_extraction_summary_used(tstore):
    class Ext:
        def get_many(self, ids):
            return {"e1": {"status": "done", "verdict": "supported",
                           "result": {"summary": "Russia struck Kharkiv overnight."}},
                    "e2": {"status": "done", "verdict": "unsupported",
                           "result": {"summary": "nope"}}}

    items = [{"id": "e1", "title": "Удар по Харкову", "summary": "Росія вдарила"},
             {"id": "e2", "title": "Удар", "summary": "Росія"}]
    annotate_events(tstore, items, extractions=Ext(), summaries=True)
    assert items[0]["summary_en"] == "Russia struck Kharkiv overnight."
    assert items[0]["title_en"] == "Russia struck Kharkiv overnight."  # fallback headline
    assert items[1]["summary_en"] is None and items[1]["title_en"] is None
