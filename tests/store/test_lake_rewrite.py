from datetime import UTC, datetime

import pyarrow as pa
import pytest

from osint.core.models import Document
from osint.store.lake import Lake, document_row

T0 = datetime(2026, 11, 21, 12, 30, tzinfo=UTC)


def _row(i, title="t"):
    d = Document(id=f"doc:{i}", source="rss:x", url=f"https://e.x/{i}", title=title,
                 snippet="s", language="en", published_at=T0, fetched_at=T0, kind="article")
    return document_row(d)


def test_read_and_rewrite_partition(tmp_path):
    lake = Lake(tmp_path)
    lake.write("documents", [_row(1)])
    lake.write("documents", [_row(2)])
    assert lake.partitions("documents") == [("2026-11-21", 12)]
    rows, files = lake.read_partition("documents", "2026-11-21", 12)
    assert len(rows) == 2 and len(files) == 2
    new = [{**r, "title": "new"} for r in rows]
    assert lake.rewrite_partition("documents", "2026-11-21", 12, new, files)
    rows2, files2 = lake.read_partition("documents", "2026-11-21", 12)
    assert len(files2) == 1 and sorted(r["id"] for r in rows2) == ["doc:1", "doc:2"]
    assert {r["title"] for r in rows2} == {"new"}
    assert not list(tmp_path.rglob("*.tmp"))
    assert lake.query("SELECT count(*) FROM documents")[0][0] == 2


def test_rewrite_keeps_files_added_meanwhile(tmp_path):
    lake = Lake(tmp_path)
    lake.write("documents", [_row(1)])
    rows, files = lake.read_partition("documents", "2026-11-21", 12)
    lake.write("documents", [_row(2)])  # an ingest flush after our read
    assert lake.rewrite_partition("documents", "2026-11-21", 12, rows, files)
    ids = sorted(r[0] for r in lake.query("SELECT id FROM documents"))
    assert ids == ["doc:1", "doc:2"]


def test_rewrite_aborts_when_inputs_vanished(tmp_path):
    lake = Lake(tmp_path)
    lake.write("documents", [_row(1)])
    rows, files = lake.read_partition("documents", "2026-11-21", 12)
    lake.write("documents", [_row(2)])
    lake.compact("documents", datetime(2026, 11, 22, tzinfo=UTC))  # removes `files`
    assert not lake.rewrite_partition("documents", "2026-11-21", 12, rows, files)
    assert lake.query("SELECT count(*) FROM documents")[0][0] == 2


def test_failed_write_leaves_partition_untouched(tmp_path):
    lake = Lake(tmp_path)
    lake.write("documents", [_row(1)])
    _, files = lake.read_partition("documents", "2026-11-21", 12)
    with pytest.raises(pa.ArrowException):
        lake.rewrite_partition("documents", "2026-11-21", 12, [{"id": 5}], files)
    assert lake.query("SELECT count(*) FROM documents")[0][0] == 1
    assert not list(tmp_path.rglob("*.tmp"))
