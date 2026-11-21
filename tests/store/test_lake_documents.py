import json
from datetime import UTC, datetime, timedelta

from osint.core.geo import h3_cells
from osint.core.models import Document, PlaceHit, TriageResult
from osint.store.lake import SCHEMAS, Lake, document_row

T0 = datetime(2026, 11, 21, 12, 30, tzinfo=UTC)


def _doc(i="doc:1", published=T0, fetched=T0):
    return Document(id=i, source="rss:bbc", url="https://e.x/a", title="Strike on depot",
                    snippet="snip", language="en", published_at=published, fetched_at=fetched,
                    kind="article", attrs={"feed": "bbc"})


HIT = PlaceHit(name="Kyiv", country="UA", lat=50.45, lon=30.52, precision="city",
               confidence=0.9, matched="Kyiv")


def test_document_row_matches_schema_and_carries_triage(tmp_path):
    t = TriageResult(language="en", relevance=0.8, categories=["armed_conflict", "political"],
                     places=[HIT], best_place=HIT)
    row = document_row(_doc(), t)
    assert set(row) == set(SCHEMAS["documents"].names)
    assert row["categories"] == "armed_conflict,political"
    assert (row["h3_3"], row["h3_5"], row["h3_7"]) == h3_cells(50.45, 30.52)
    assert json.loads(row["places"])[0]["name"] == "Kyiv"
    lake = Lake(tmp_path)
    lake.write("documents", [row])
    # partitioned by published_at, not fetched_at
    assert (tmp_path / "documents" / "date=2026-11-21" / "hour=12").is_dir()
    got = lake.query("SELECT id, relevance, categories, lat, place_precision FROM documents")
    assert got[0][0] == "doc:1" and abs(got[0][1] - 0.8) < 1e-6
    assert got[0][2:] == ("armed_conflict,political", 50.45, "city")


def test_document_row_without_place_or_triage(tmp_path):
    t = TriageResult(language="en", relevance=0.0)
    row = document_row(_doc(), t)
    assert row["lat"] is None and row["h3_5"] is None and row["places"] == "[]"
    bare = document_row(_doc("doc:2"))
    assert bare["relevance"] is None and bare["categories"] == ""
    lake = Lake(tmp_path)
    lake.write("documents", [row, bare])
    assert lake.query("SELECT count(*), count(lat) FROM documents") == [(2, 0)]


def test_empty_documents_dataset_and_index(tmp_path):
    lake = Lake(tmp_path)
    assert lake.query("SELECT count(*) FROM documents") == [(0,)]
    assert lake.recent_document_index(T0) == {}


def test_recent_document_index_uses_fetched_at(tmp_path):
    lake = Lake(tmp_path)
    old_pub = _doc("doc:old-pub", published=T0 - timedelta(days=5), fetched=T0)
    old_fetch = _doc("doc:old-fetch", published=T0 - timedelta(days=5),
                     fetched=T0 - timedelta(days=4))
    lake.write("documents", [document_row(old_pub), document_row(old_fetch)])
    index = lake.recent_document_index(T0 - timedelta(days=1))
    assert index == {"doc:old-pub": T0}
