from datetime import UTC, datetime, timedelta

import pytest

from osint.core.models import TrackInfo
from osint.store.sqlite import SqliteStore

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "t.sqlite")
    yield s
    s.close()


def test_cursor_roundtrip(store):
    assert store.get_cursor("usgs") is None
    store.set_cursor("usgs", {"last": "x"})
    store.set_cursor("usgs", {"last": "y"})
    assert store.get_cursor("usgs") == {"last": "y"}


def test_upsert_tracks_merges_fields(store):
    store.upsert_tracks([TrackInfo(track_id="air:a", kind="air", ident="a", name="RCH1",
                                   extra={"desc": "C-17"})], T0)
    store.upsert_tracks([TrackInfo(track_id="air:a", kind="air", ident="a", is_mil=True,
                                   extra={"squawk": "7700"})], T0 + timedelta(minutes=5))
    t = store.get_track("air:a")
    assert t["name"] == "RCH1"
    assert t["is_mil"] == 1
    assert t["extra"] == {"desc": "C-17", "squawk": "7700"}
    assert t["first_seen"] == T0.isoformat()
    assert t["last_seen"] == (T0 + timedelta(minutes=5)).isoformat()


def test_quarantine_and_sources(store):
    store.quarantine("adsb", "position", "lat out of range", {"lat": 99})
    assert store.quarantine_count() == 1
    store.register_source("adsb", "aviation", "mixed", False)
    store.register_source("adsb", "aviation", "mixed-v2", False)
    assert [(s["name"], s["licence"], s["commercial_ok"]) for s in store.list_sources()] == [
        ("adsb", "mixed-v2", 0)
    ]


def test_get_tracks_chunked(store):
    tracks = [TrackInfo(track_id=f"air:{i}", kind="air", ident=str(i), extra={"k": "v"})
              for i in range(1000)]
    store.upsert_tracks(tracks, T0)
    got = store.get_tracks([t.track_id for t in tracks] + ["air:missing"])
    assert len(got) == 1000
    assert got["air:5"]["extra"] == {"k": "v"}
    assert store.get_tracks([]) == {}
