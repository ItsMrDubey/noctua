import itertools
from datetime import UTC, date, datetime, timedelta

import pytest

from osint.backfill.common import (
    BackfillStore,
    Checkpoint,
    TokenBucket,
    Window,
    day_units,
    fixed_chunks,
    month_chunks,
    redact,
    resolve_window,
    split_half,
)
from osint.backfill.status import backfill_status, summarize

NOW = datetime(2026, 11, 21, 15, 30, tzinfo=UTC)


def test_resolve_window_days_and_since_until():
    w = resolve_window(days=30, since=None, until=None, default_days=7, now=NOW)
    assert w == Window(NOW - timedelta(days=30), NOW)
    w = resolve_window(days=None, since=date(2026, 1, 1), until=date(2026, 1, 31),
                       default_days=7, now=NOW)
    assert w.start == datetime(2026, 1, 1, tzinfo=UTC)
    assert w.end == datetime(2026, 2, 1, tzinfo=UTC)  # --until is inclusive
    w = resolve_window(days=None, since=None, until=None, default_days=7, now=NOW)
    assert w.end - w.start == timedelta(days=7)
    w = resolve_window(days=None, since=None, until=None, default_days=7, now=NOW,
                       default_since=date(1989, 1, 1))
    assert w.start.year == 1989 and w.end == NOW
    # --until in the future is clipped to now
    w = resolve_window(days=None, since=date(2026, 11, 1), until=date(2027, 1, 1),
                       default_days=7, now=NOW)
    assert w.end == NOW
    with pytest.raises(ValueError):
        resolve_window(days=3, since=date(2026, 1, 1), until=None, default_days=7, now=NOW)
    with pytest.raises(ValueError):
        resolve_window(days=0, since=None, until=None, default_days=7, now=NOW)


def test_day_units_cut_at_midnight():
    w = Window(datetime(2026, 11, 19, 15, tzinfo=UTC), datetime(2026, 11, 21, 6, tzinfo=UTC))
    units = day_units(w)
    assert [(u.start.isoformat(), u.end.isoformat()) for u in units] == [
        ("2026-11-19T15:00:00+00:00", "2026-11-20T00:00:00+00:00"),
        ("2026-11-20T00:00:00+00:00", "2026-11-21T00:00:00+00:00"),
        ("2026-11-21T00:00:00+00:00", "2026-11-21T06:00:00+00:00"),
    ]


def test_month_and_fixed_chunks_cover_window_exactly():
    w = Window(datetime(2025, 11, 21, 15, tzinfo=UTC), NOW)
    months = month_chunks(w)
    assert len(months) == 13
    assert months[0].start == w.start and months[-1].end == w.end
    assert months[1].start == datetime(2025, 12, 1, tzinfo=UTC)
    assert all(a.end == b.start for a, b in itertools.pairwise(months))
    chunks = fixed_chunks(Window(datetime(2026, 1, 1, tzinfo=UTC),
                                 datetime(2026, 1, 25, tzinfo=UTC)), timedelta(days=10))
    assert [(c.end - c.start).days for c in chunks] == [10, 10, 4]


def test_split_half_and_empty_window():
    a, b = split_half(Window(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)))
    assert a.end == b.start == datetime(2026, 1, 1, 12, tzinfo=UTC)
    with pytest.raises(ValueError):
        Window(NOW, NOW)


def test_redact():
    assert redact("GET /api/area/csv/SECRET123/MODIS failed SECRET123", "SECRET123") == \
        "GET /api/area/csv/***/MODIS failed ***"
    assert redact("nothing", None, "") == "nothing"


async def test_token_bucket_waits_for_refill():
    t = [0.0]
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)
        t[0] += s

    bucket = TokenBucket(10, 600, clock=lambda: t[0], sleep=sleep)  # 1 token / 60 s
    await bucket.acquire(10)
    assert slept == []
    await bucket.acquire(2)  # empty: waits 120 s of refill
    assert slept == [pytest.approx(120)]
    t[0] += 6000  # refill caps at capacity
    assert bucket.wait_time(10) == 0
    with pytest.raises(ValueError):
        await bucket.acquire(11)


def test_checkpoint_resume_and_status(tmp_path):
    store = BackfillStore(tmp_path / "s.sqlite")
    cp = Checkpoint(store, "usgs")
    cp.start(Window(NOW - timedelta(days=2), NOW))
    cp.add_rows("observations", 5)
    cp.mark_done("2026-11-19")
    cp.error("boom")
    # a fresh process sees the finished units and cumulative rows
    cp2 = Checkpoint(store, "usgs")
    assert cp2.is_done("2026-11-19") and not cp2.is_done("2026-11-20")
    cp2.start(None)
    cp2.add_rows("observations", 3)
    cp2.mark_done("2026-11-20")
    cp2.finish()
    st = store.get_cursor("backfill:usgs")
    assert st["rows"] == {"observations": 8} and st["run_rows"] == {"observations": 3}
    assert st["state"] == "finished" and st["last_checkpoint"] == "2026-11-20"
    status = backfill_status(store)["sources"]
    assert status["usgs"]["units_done"] == 2 and status["usgs"]["errors"][0]["message"] == "boom"
    assert status["gdelt"] == {"state": "never_run"}
    store.close()


def test_dry_run_checkpoint_writes_nothing(tmp_path):
    store = BackfillStore(tmp_path / "s.sqlite")
    cp = Checkpoint(store, "gdelt", dry_run=True)
    cp.start(None)
    cp.mark_done("x")
    assert store.get_cursor("backfill:gdelt") is None
    store.close()


def test_status_detects_stale_running():
    old = (NOW - timedelta(hours=2)).isoformat()
    assert summarize({"state": "running", "updated_at": old}, NOW)["state"] == "stale"
    fresh = (NOW - timedelta(minutes=1)).isoformat()
    s = summarize({"state": "running", "updated_at": fresh}, NOW)
    assert s["running"] is True
