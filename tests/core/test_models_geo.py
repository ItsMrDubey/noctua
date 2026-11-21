from datetime import UTC, datetime

import h3

from osint.core.geo import H3_RESOLUTIONS, h3_cells, haversine_km, utcnow, valid_coord
from osint.core.models import Batch, Observation, Position, TrackInfo


def test_h3_cells_returns_one_int_per_resolution():
    cells = h3_cells(51.5, -0.12)
    assert len(cells) == 3
    assert [h3.get_resolution(h3.int_to_str(c)) for c in cells] == list(H3_RESOLUTIONS)


def test_haversine_london_paris():
    assert abs(haversine_km(51.5074, -0.1278, 48.8566, 2.3522) - 343.5) < 2


def test_valid_coord():
    assert valid_coord(0, 0)
    assert not valid_coord(91, 0)
    assert not valid_coord(0, 181)
    assert not valid_coord(float("nan"), 0)
    assert not valid_coord(None, 0)


def test_utcnow_is_aware():
    assert utcnow().tzinfo is UTC


def test_batch_size_counts_all_records():
    ts = datetime(2026, 11, 21, tzinfo=UTC)
    b = Batch(
        source="t",
        positions=[Position(track_id="air:a", kind="air", ts=ts, lat=1, lon=2, source="t")],
        tracks=[TrackInfo(track_id="air:a", kind="air", ident="a")],
        observations=[Observation(id="x", type="fire", ts=ts, lat=1, lon=2, source="t")],
    )
    assert b.size() == 3
    assert Batch(source="t").size() == 0
