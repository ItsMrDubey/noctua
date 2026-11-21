from datetime import UTC, datetime

import msgspec
import pytest

from osint.core.models import (
    SNIPPET_MAX,
    Batch,
    Document,
    PlaceHit,
    TriageResult,
    clip_snippet,
    document_id,
    normalise_url,
)

T = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def test_normalise_url_strips_noise():
    assert normalise_url("HTTP://WWW.Example.com/a/b/?utm_source=x&b=2&a=1#frag") == \
        "https://example.com/a/b?a=1&b=2"
    assert normalise_url("https://x.org/p?traffic_source=rss") == "https://x.org/p"


def test_document_id_is_stable_and_url_based():
    a = document_id("https://www.aljazeera.com/news/x?traffic_source=rss")
    assert a == document_id("http://aljazeera.com/news/x/")
    assert a.startswith("doc:") and len(a) == 4 + 16
    assert document_id("https://e.x/a", "123") != document_id("https://e.x/a", "124")


def test_clip_snippet():
    assert clip_snippet("  a \n b  ") == "a b"
    assert clip_snippet(None) == ""
    long = clip_snippet("word " * 200)
    assert len(long) <= SNIPPET_MAX and long.endswith("…")


def test_document_roundtrip_and_batch_size():
    d = Document(id=document_id("https://e.x/a"), source="rss:x", url="https://e.x/a",
                 title="T", snippet="S", language="en", published_at=T, fetched_at=T,
                 kind="article", attrs={"feed": "x"})
    back = msgspec.json.decode(msgspec.json.encode(d), type=Document)
    assert back == d and back.published_at.tzinfo is not None
    with pytest.raises(AttributeError):
        d.title = "changed"  # frozen
    assert Batch(source="x", documents=[d, d]).size() == 2


def test_triage_result_roundtrip():
    hit = PlaceHit(name="Kyiv", country="UA", lat=50.45, lon=30.52, precision="city",
                   confidence=0.9, geonameid=703448)
    r = TriageResult(language="uk", relevance=0.8, categories=["armed_conflict"],
                     places=[hit], best_place=hit)
    assert msgspec.json.decode(msgspec.json.encode(r), type=TriageResult) == r
