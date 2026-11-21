"""Partition pruning, compaction and the perf regression guard for the Parquet lake."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq

from osint.core.models import Position
from osint.store.lake import SCHEMAS, Lake, position_row

BASE = datetime(2026, 11, 1, tzinfo=UTC)


def _row(ts, tid="air:a"):
    return position_row(Position(track_id=tid, kind="air", ts=ts, lat=10, lon=20, source="t"))


def _files(lake, dataset="positions"):
    return sorted((lake.root / dataset).rglob("*.parquet"))


def test_query_range_prunes_to_overlapping_partitions(tmp_path):
    lake = Lake(tmp_path)
    for h in range(6):
        lake.write("positions", [_row(BASE + timedelta(hours=h, minutes=30), f"air:{h}")])
    s, e = BASE + timedelta(hours=2), BASE + timedelta(hours=3, minutes=59)
    files = lake.partition_files("positions", s, e)
    assert {f.parent.name for f in files} == {"hour=02", "hour=03"}
    sql = "SELECT track_id FROM positions WHERE ts >= ? AND ts <= ? ORDER BY 1"
    assert lake.query(sql, [s, e], start=s, end=e) == [("air:2",), ("air:3",)]
    # whole-dataset fallback still sees everything; hive columns are exposed
    assert lake.query("SELECT count(*), min(hour) FROM positions") == [(6, "00")]


def test_query_range_with_no_matching_partition_is_empty_with_hive_columns(tmp_path):
    lake = Lake(tmp_path)
    lake.write("positions", [_row(BASE)])
    s = BASE + timedelta(days=5)
    assert lake.query("SELECT count(*), max(hour), max(date) FROM positions",
                      start=s, end=s + timedelta(hours=1)) == [(0, None, None)]
    # placeholder for a dataset with no files at all also carries date/hour
    assert Lake(tmp_path / "x").query("SELECT count(*), max(hour) FROM observations") == [(0, None)]


def test_compact_merges_closed_partitions_and_dedupes(tmp_path):
    lake = Lake(tmp_path)
    for i in range(5):
        lake.write("positions", [_row(BASE + timedelta(minutes=i), f"air:{i}")])
    lake.write("positions", [_row(BASE, "air:0")])  # exact duplicate (partial flush retry)
    now_hour = BASE + timedelta(hours=3, minutes=5)
    lake.write("positions", [_row(now_hour, "air:x"), _row(now_hour, "air:y")])
    lake.write("positions", [_row(now_hour, "air:z")])
    assert lake.compact("positions", older_than=BASE + timedelta(hours=3)) == 1
    closed = list((tmp_path / "positions" / "date=2026-11-01" / "hour=00").glob("*.parquet"))
    assert len(closed) == 1 and closed[0].name.startswith("compact-")
    assert len(list((tmp_path / "positions" / "date=2026-11-01" / "hour=03").glob("*"))) == 2
    assert lake.query("SELECT count(*) FROM positions WHERE hour = '00'") == [(5,)]
    assert not list(tmp_path.rglob("*.tmp"))
    # idempotent
    assert lake.compact("positions", older_than=BASE + timedelta(hours=3)) == 0


def test_compact_rerun_after_crash_does_not_double_rows(tmp_path, monkeypatch):
    lake = Lake(tmp_path)
    for i in range(3):
        lake.write("positions", [_row(BASE + timedelta(minutes=i), f"air:{i}")])
    def crash(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr("osint.store.lake._remove", crash)
    try:
        lake.compact("positions", older_than=BASE + timedelta(hours=2))
    except KeyboardInterrupt:
        pass
    monkeypatch.undo()
    assert len(_files(lake)) == 4  # merged file + the 3 inputs left behind
    lake.compact("positions", older_than=BASE + timedelta(hours=2))
    assert len(_files(lake)) == 1
    assert lake.query("SELECT count(*) FROM positions") == [(3,)]


def test_compact_does_not_touch_current_hour(tmp_path):
    lake = Lake(tmp_path)
    lake.write("positions", [_row(BASE)])
    lake.write("positions", [_row(BASE + timedelta(minutes=1))])
    assert lake.compact("positions", older_than=BASE + timedelta(minutes=50)) == 0
    assert len(_files(lake)) == 2


def test_compact_is_serialised_with_writes(tmp_path):
    lake = Lake(tmp_path)
    for i in range(3):
        lake.write("positions", [_row(BASE + timedelta(minutes=i), f"air:{i}")])
    done = []
    with lake.write_lock:
        t = threading.Thread(target=lambda: done.append(
            lake.compact("positions", older_than=BASE + timedelta(hours=2))))
        t.start()
        t.join(0.3)
        assert not done
    t.join(5)
    assert done == [1]


def test_lake_reuses_one_connection(tmp_path):
    lake = Lake(tmp_path)
    lake.query("SELECT 1")
    con = lake._con
    lake.query("SELECT count(*) FROM positions")
    assert lake._con is con
    lake.close()


def _write_file(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMAS["positions"]), path)


def test_perf_many_small_files(tmp_path):
    """2,000 small files over 48 hourly partitions: 1 h query < 1 s; compaction -> <= 48 files."""
    lake = Lake(tmp_path)
    template = _row(BASE)
    jobs = []
    for i in range(2000):
        h = i % 48
        ts = BASE + timedelta(hours=h, seconds=i)
        rows = [dict(template, ts=ts, track_id=f"air:{i}"), dict(template, ts=ts, track_id=f"b:{i}")]
        if i % 100 == 0:
            rows.append(dict(rows[0]))  # injected exact duplicate
        p = tmp_path / "positions" / f"date={ts:%Y-%m-%d}" / f"hour={ts:%H}" / f"part-{i}.parquet"
        jobs.append((p, rows))
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda j: _write_file(*j), jobs))
    s, e = BASE + timedelta(hours=20), BASE + timedelta(hours=21)
    sql = "SELECT count(*) FROM positions WHERE ts >= ? AND ts < ?"
    t0 = time.perf_counter()
    expected = lake.query(sql, [s, e], start=s, end=e)
    assert time.perf_counter() - t0 < 1.0
    assert expected == [(42 * 2 + 2,)]  # 42 files in hour 20; i=500 and 1700 add a duplicate
    total = lake.query("SELECT count(*) FROM positions")[0][0]
    assert total == 4000 + 20
    lake.compact("positions", older_than=BASE + timedelta(days=3))
    assert len(_files(lake)) <= 48
    assert lake.query("SELECT count(*) FROM positions") == [(4000,)]
    assert lake.query("SELECT count(DISTINCT track_id) FROM positions") == [(4000,)]
    assert lake.query(sql, [s, e], start=s, end=e) == [(84,)]
