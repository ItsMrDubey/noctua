from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from osint.core.models import Observation, Position
from osint.store.lake import Lake, observation_row, position_row

T0 = datetime(2026, 11, 21, 12, 30, tzinfo=UTC)


def _pos(ts):
    return Position(track_id="air:a", kind="air", ts=ts, lat=10, lon=20, source="t", alt_ft=1000)


def test_write_partitions_by_hour_and_is_queryable(tmp_path):
    lake = Lake(tmp_path)
    rows = [position_row(_pos(T0)), position_row(_pos(T0 + timedelta(hours=1)))]
    assert lake.write("positions", rows) == 2
    assert (tmp_path / "positions" / "date=2026-11-21" / "hour=12").is_dir()
    assert (tmp_path / "positions" / "date=2026-11-21" / "hour=13").is_dir()
    assert not list(tmp_path.rglob("*.tmp"))
    assert lake.query("SELECT count(*), max(alt_ft) FROM positions") == [(2, 1000)]


def test_empty_dataset_query_returns_zero(tmp_path):
    assert Lake(tmp_path).query("SELECT count(*) FROM observations") == [(0,)]


def test_recent_observation_index(tmp_path):
    lake = Lake(tmp_path)
    old = Observation(id="fire:1", type="fire", ts=T0 - timedelta(days=3), lat=1, lon=1, source="t")
    new = Observation(id="quake:2", version="v9", type="quake", ts=T0, lat=1, lon=1, source="t",
                      attrs={"place": "x"})
    lake.write("observations", [observation_row(old), observation_row(new)])
    index = lake.recent_observation_index(T0 - timedelta(days=1))
    assert index == {("quake:2", "v9"): T0}
    assert index[("quake:2", "v9")].tzinfo is not None
    assert lake.query("SELECT attrs FROM observations WHERE id='quake:2'") == [('{"place":"x"}',)]


def test_partition_uses_utc_for_offset_timestamps(tmp_path):
    lake = Lake(tmp_path)
    ts = datetime(2026, 11, 21, 2, 30, tzinfo=timezone(timedelta(hours=5)))  # 20th 21:30 UTC
    lake.write("positions", [position_row(_pos(ts))])
    assert (tmp_path / "positions" / "date=2026-11-20" / "hour=21").is_dir()
    assert lake.query("SELECT count(*) FROM positions") == [(1,)]


def test_query_works_when_path_contains_apostrophe(tmp_path):
    lake = Lake(tmp_path / "o'brien")
    lake.write("positions", [position_row(_pos(T0))])
    assert lake.query("SELECT count(*) FROM positions") == [(1,)]


def test_write_failure_leaves_no_tmp_file(tmp_path, monkeypatch):
    import pyarrow.parquet as pq
    import pytest

    lake = Lake(tmp_path)

    def boom(table, where, **kw):
        Path(where).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(pq, "write_table", boom)
    row = position_row(_pos(datetime(2026, 1, 1, 5, tzinfo=UTC)))
    with pytest.raises(OSError):
        lake.write("positions", [row])
    assert list(tmp_path.rglob("*.tmp")) == []


def test_duckdb_connection_is_memory_capped(tmp_path):
    lake = Lake(tmp_path, duckdb_memory_limit="1GiB", duckdb_threads=2)
    con = lake._connect()
    try:
        assert con.execute("SELECT current_setting('memory_limit')").fetchone()[0] == "1.0 GiB"
        assert con.execute("SELECT current_setting('threads')").fetchone()[0] == 2
    finally:
        con.close()


def test_concurrent_queries_do_not_conflict_on_view_names(tmp_path):
    """Regression: a shared view name per dataset made parallel queries fail with a DuckDB
    write-write catalog conflict (the quakes layer returned 500 next to the fires layer)."""
    import threading

    lake = Lake(tmp_path)
    obs = [Observation(id=f"quake:{i}", type="quake", ts=T0, lat=1, lon=1, source="t") for i in range(5)]
    lake.write("observations", [observation_row(o) for o in obs])
    lake.write("positions", [position_row(_pos(T0))])
    errors: list[BaseException] = []

    def run(sql: str) -> None:
        try:
            for _ in range(5):
                lake.query(sql, start=T0 - timedelta(hours=1), end=T0 + timedelta(hours=1))
        except BaseException as e:  # noqa: BLE001 - collected for the assertion below
            errors.append(e)

    threads = [threading.Thread(target=run, args=(sql,)) for sql in
               ["SELECT count(*) FROM observations", "SELECT count(*) FROM positions"] * 4]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert lake.query("SELECT count(*) FROM observations") == [(5,)]
