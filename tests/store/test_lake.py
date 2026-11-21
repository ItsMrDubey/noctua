from datetime import UTC, datetime, timedelta

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


def test_recent_observation_keys(tmp_path):
    lake = Lake(tmp_path)
    old = Observation(id="fire:1", type="fire", ts=T0 - timedelta(days=3), lat=1, lon=1, source="t")
    new = Observation(id="quake:2", version="v9", type="quake", ts=T0, lat=1, lon=1, source="t",
                      attrs={"place": "x"})
    lake.write("observations", [observation_row(old), observation_row(new)])
    assert lake.recent_observation_keys(T0 - timedelta(days=1)) == {("quake:2", "v9")}
    assert lake.query("SELECT attrs FROM observations WHERE id='quake:2'") == [('{"place":"x"}',)]
