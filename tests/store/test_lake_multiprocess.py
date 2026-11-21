"""Lake writes/compaction/rewrites are safe across processes (OS file lock per dataset)."""

import multiprocessing as mp
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osint.store.lake import LOCK_NAME, SCHEMAS, FileLock, Lake, merge_files

BASE = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)


def _pos(ts: datetime, track: str) -> dict:
    return {"track_id": track, "kind": "air", "ts": ts, "lat": 1.0, "lon": 2.0, "alt_ft": 100,
            "speed_kts": 1.0, "heading_deg": 0.0, "source": "t", "h3_3": 1, "h3_5": 2,
            "h3_7": 3}


def _writer(root: str, tag: str, n: int) -> None:
    lake = Lake(Path(root))
    for i in range(n):
        lake.write("positions", [_pos(BASE + timedelta(minutes=i % 50), f"{tag}:{i}")])
        if i % 5 == 4:  # compaction races the other process's writes
            lake.compact("positions", older_than=BASE + timedelta(hours=2))


def _hold_lock(root: str, ready, release) -> None:
    with FileLock(Path(root) / "positions" / LOCK_NAME):
        ready.set()
        release.wait(20)


def test_two_processes_write_and_compact_concurrently(tmp_path):
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(str(tmp_path), tag, 30)) for tag in "ab"]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    lake = Lake(tmp_path)
    lake.compact("positions", older_than=BASE + timedelta(hours=2))
    assert lake.query("SELECT count(*), count(DISTINCT track_id) FROM positions") == [(60, 60)]
    assert not list(tmp_path.rglob("*.tmp"))
    assert len(list(tmp_path.rglob("*.parquet"))) == 1
    lake.close()


def test_lock_excludes_other_processes(tmp_path):
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold_lock, args=(str(tmp_path), ready, release))
    p.start()
    try:
        assert ready.wait(60)
        lake = Lake(tmp_path)
        lake.lock_timeout = 0.3
        t0 = time.monotonic()
        try:
            lake.write("positions", [_pos(BASE, "x")])
            raise AssertionError("write must wait for the other process's lock")
        except TimeoutError:
            assert time.monotonic() - t0 >= 0.25
        assert not list(tmp_path.rglob("*.parquet"))
    finally:
        release.set()
        p.join(30)
    lake.lock_timeout = 30
    lake.write("positions", [_pos(BASE, "x")])  # free again
    assert lake.query("SELECT count(*) FROM positions") == [(1,)]


def test_compaction_keeps_fresh_tmp_files_of_other_writers(tmp_path):
    lake = Lake(tmp_path)
    for i in range(2):
        lake.write("positions", [_pos(BASE + timedelta(minutes=i), f"t{i}")])
    part = next((tmp_path / "positions").rglob("hour=00"))
    fresh, stale = part / "part-x.parquet.99-a.tmp", part / "part-y.parquet.98-b.tmp"
    fresh.write_bytes(b"in progress")
    stale.write_bytes(b"crashed")
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    assert lake.compact("positions", older_than=BASE + timedelta(hours=2)) == 1
    assert fresh.exists() and not stale.exists()


def _doc(i: str, title: str) -> dict:
    return {"id": i, "source": "s", "kind": "article", "origin_id": "", "url": "u",
            "title": title, "snippet": "", "language": "en", "published_at": BASE,
            "fetched_at": BASE, "content_hash": "", "attrs": "{}", "relevance": 0.5,
            "categories": "", "places": "[]", "place_name": None, "place_precision": None,
            "place_confidence": None, "lat": None, "lon": None, "h3_3": None, "h3_5": None,
            "h3_7": None}


def _write(path: Path, rows: list[dict], dataset: str, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMAS[dataset]), path)
    os.utime(path, (mtime, mtime))
    return path


def test_documents_dedupe_by_id_newest_file_wins(tmp_path):
    """Partial delete after a rewrite: the stale original and its rewritten row must not
    both survive the next merge."""
    now = time.time()
    old = _write(tmp_path / "part-1.parquet", [_doc("d1", "old"), _doc("d2", "keep")],
                 "documents", now - 100)
    new = _write(tmp_path / "compact-x.parquet", [_doc("d1", "new")], "documents", now)
    t = merge_files("documents", [old, new])
    assert sorted(zip(t.column("id").to_pylist(), t.column("title").to_pylist(),
                      strict=True)) == [("d1", "new"), ("d2", "keep")]
    # positions keep exact-row de-duplication (same key, different values both kept)
    a = _write(tmp_path / "a.parquet", [_pos(BASE, "x")], "positions", now - 5)
    b = _write(tmp_path / "b.parquet", [_pos(BASE, "x"), {**_pos(BASE, "x"), "lat": 5.0}],
               "positions", now)
    assert merge_files("positions", [a, b]).num_rows == 2


def test_lake_memory_defaults(tmp_path):
    lake = Lake(tmp_path)
    assert lake.duckdb_memory_limit == os.environ.get("OSINT_DUCKDB_MEMORY_LIMIT", "512MB")
    assert lake.duckdb_threads == int(os.environ.get("OSINT_DUCKDB_THREADS", "2"))


def test_iter_query_streams_in_batches(tmp_path):
    lake = Lake(tmp_path)
    lake.write("positions", [_pos(BASE + timedelta(seconds=i), f"t{i}") for i in range(25)])
    batches = list(lake.iter_query("SELECT track_id FROM positions ORDER BY ts", batch=10))
    assert [len(b) for b in batches] == [10, 10, 5]
    assert batches[0][0] == ("t0",)
    lake.close()
