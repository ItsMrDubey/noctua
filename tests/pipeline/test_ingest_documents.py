from datetime import datetime, timedelta

import pytest

from osint.core.geo import utcnow
from osint.core.metrics import Metrics
from osint.core.models import Batch, Document, document_id
from osint.enrich.gazetteer import Gazetteer
from osint.pipeline.ingest import Ingestor
from osint.pipeline.triage import DEFAULT_CONFIG, Triage
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore
from tests.gazetteer_fixture import write_gazetteer


@pytest.fixture
def parts(tmp_path):
    store = SqliteStore(tmp_path / "s.sqlite")
    lake = Lake(tmp_path / "lake")
    yield store, lake, Metrics()
    store.close()


@pytest.fixture(scope="module")
def triage(tmp_path_factory):
    gz = Gazetteer.load(write_gazetteer(tmp_path_factory.mktemp("g")))
    return Triage.from_file(DEFAULT_CONFIG, gz)


def _doc(url="https://e.x/a", title="Russian missile strike hits Kharkiv", published=None,
         fetched=None, doc_id=None):
    now = utcnow()
    return Document(id=doc_id if doc_id is not None else document_id(url), source="t", url=url,
                    title=title, snippet="", language="",
                    published_at=published or now - timedelta(minutes=5),
                    fetched_at=fetched or now, kind="article")


def test_documents_triaged_deduped_and_written(parts, triage):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics, triage=triage)
    d = _doc()
    same_article = _doc(url="https://www.e.x/a/?utm_source=feed")
    other = _doc(url="https://e.x/b", title="Apple unveils new phone")
    ing.process(Batch(source="t", documents=[d, same_article, other]))
    ing.process(Batch(source="t", documents=[d]))
    ing.flush()
    rows = lake.query("SELECT url, categories, place_name, h3_5 IS NOT NULL, relevance, "
                      "language FROM documents ORDER BY url")
    assert [r[0] for r in rows] == ["https://e.x/a", "https://e.x/b"]
    assert rows[0][1:4] == ("armed_conflict", "Kharkiv", True) and rows[0][4] > 0.5
    assert rows[0][5] == "en"  # detected by triage
    assert rows[1][1] == "" and rows[1][4] == 0
    assert metrics.get("documents_duplicate_total", source="t") == 2
    assert metrics.get("documents_triaged_total", source="t") == 2
    assert metrics.get("documents_relevant_total", category="armed_conflict") == 1


def test_invalid_documents_quarantined_and_stale_skipped(parts, triage):
    store, lake, metrics = parts
    ing = Ingestor(store, lake, metrics, triage=triage)
    now = utcnow()
    ing.process(Batch(source="t", documents=[
        _doc(url="", doc_id="doc:nourl"),
        _doc(url="https://e.x/future", published=now + timedelta(hours=1)),
        _doc(url="https://e.x/naive", published=datetime(2026, 1, 1)),
        _doc(url="https://e.x/stale", published=now - timedelta(days=5)),
    ]))
    ing.flush()
    assert store.quarantine_count() == 3
    assert metrics.get("records_quarantined_total", source="t", type="document") == 3
    assert metrics.get("documents_stale_total", source="t") == 1
    assert lake.query("SELECT count(*) FROM documents") == [(0,)]


def test_documents_warm_start_and_prune(parts, triage):
    store, lake, metrics = parts
    d = _doc()
    first = Ingestor(store, lake, metrics, triage=triage)
    first.process(Batch(source="t", documents=[d]))
    first.flush()
    second = Ingestor(store, lake, metrics, triage=triage)
    second.warm_start(utcnow() - timedelta(hours=48))
    second.process(Batch(source="t", documents=[d]))
    second.flush()
    assert lake.query("SELECT count(*) FROM documents") == [(1,)]
    # entries stay at least doc_max_age even if the caller prunes more aggressively
    second.prune(utcnow() - timedelta(minutes=1))
    assert d.id in second._seen_docs
    second._seen_docs[d.id] = utcnow() - timedelta(days=3)
    second.prune(utcnow() - timedelta(hours=48))
    assert d.id not in second._seen_docs


def test_failed_triage_quarantines_batch_and_keeps_running(parts):
    store, lake, metrics = parts

    class Boom:
        def assess(self, doc):
            raise RuntimeError("triage exploded")

    ing = Ingestor(store, lake, metrics, triage=Boom())
    ing._process_safe(Batch(source="t", documents=[_doc()]))
    assert store.quarantine_count() == 1
    assert metrics.get("ingest_errors_total") == 1


def test_triage_created_lazily(parts, monkeypatch):
    store, lake, metrics = parts
    made = []

    def fake_default():
        made.append(1)
        return "triage"

    monkeypatch.setattr(Triage, "default", staticmethod(fake_default))
    ing = Ingestor(store, lake, metrics)
    ing.process(Batch(source="t"))
    assert made == []  # no documents, nothing loaded
    assert ing.triage == "triage" and ing.triage == "triage"
    assert made == [1]
