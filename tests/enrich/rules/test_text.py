import numpy as np

from osint.enrich.rules.base import ExtractDoc
from osint.enrich.rules.text import cut_summary, headline_summary, pick_document


def _d(i, title, *, role="mention", at="2026-11-25T10:00:00.000+00:00", vec=None, snippet=""):
    return ExtractDoc(doc_id=f"d{i}", title=title, snippet=snippet, role=role,
                      published_at=at, vector=vec)


def test_primary_document_without_embeddings():
    docs = [_d(1, "Early mention", at="2026-11-25T09:00:00.000+00:00"),
            _d(2, "Primary report", role="primary")]
    assert pick_document({}, docs).doc_id == "d2"


def test_earliest_document_when_there_is_no_primary():
    docs = [_d(1, "Later", at="2026-11-25T11:00:00.000+00:00"),
            _d(2, "Earlier", at="2026-11-25T08:00:00.000+00:00"), _d(3, "  ")]
    assert pick_document({}, docs).doc_id == "d2"
    assert pick_document({}, [_d(1, "")]) is None and pick_document({}, []) is None


def test_document_nearest_the_centroid_wins():
    centroid = np.array([1.0, 0.0])
    docs = [_d(1, "Off topic", role="primary", vec=np.array([0.0, 1.0])),
            _d(2, "On topic", vec=np.array([0.9, 0.1])), _d(3, "No vector")]
    assert pick_document({"vector": centroid}, docs).doc_id == "d2"
    no_vectors = [_d(1, "A", role="primary"), _d(2, "B")]
    assert pick_document({"vector": centroid}, no_vectors).doc_id == "d1"


def test_cut_summary_at_a_sentence_boundary():
    assert cut_summary("  Short   text. ") == "Short text."
    long = "Sentence number one is here. " * 20
    out = cut_summary(long)
    assert len(out) <= 280 and out.endswith(".") and out.count(".") == 9
    words = cut_summary("word " * 100)
    assert len(words) <= 280 and words.endswith("…") and not words.endswith(" …")


def test_headline_and_summary_values():
    docs = [_d(1, "Missile hits Kharkiv", role="primary",
               snippet="A missile hit Kharkiv overnight. Officials said two people died.")]
    vals = headline_summary({}, docs)
    assert [(v.field, v.value, v.doc_id) for v in vals] == [
        ("headline", "Missile hits Kharkiv", "d1"),
        ("summary", "A missile hit Kharkiv overnight. Officials said two people died.", "d1")]
    assert vals[0].quote == "Missile hits Kharkiv"
    assert [v.field for v in headline_summary({}, [_d(1, "Title only")])] == ["headline"]
