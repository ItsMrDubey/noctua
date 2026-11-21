"""Lake thread safety: queries, compaction and close() from several threads at once."""

import threading
import time
from datetime import UTC, datetime, timedelta

from osint.core.models import Position
from osint.store.lake import Lake, position_row

BASE = datetime(2026, 11, 1, tzinfo=UTC)


def _rows(h, n=3):
    return [position_row(Position(track_id=f"air:{h}:{i}", kind="air",
                                  ts=BASE + timedelta(hours=h, minutes=i), lat=1, lon=2,
                                  source="t")) for i in range(n)]


def test_close_is_idempotent(tmp_path):
    lake = Lake(tmp_path)
    lake.write("positions", _rows(0))
    assert lake.query("SELECT count(*) FROM positions") == [(3,)]
    lake.close()
    lake.close()
    assert lake.query("SELECT count(*) FROM positions") == [(3,)]  # reconnects lazily
    lake.close()


def test_close_waits_for_in_flight_queries(tmp_path, monkeypatch):
    lake = Lake(tmp_path)
    lake.write("positions", _rows(0))
    lake.query("SELECT 1")
    entered, release = threading.Event(), threading.Event()
    real_bind = Lake._bind

    def slow_bind(self, cur, sql, start, end):
        entered.set()
        release.wait(5)
        return real_bind(self, cur, sql, start, end)

    monkeypatch.setattr(Lake, "_bind", slow_bind)
    result: list = []
    t = threading.Thread(target=lambda: result.append(
        lake.query("SELECT count(*) FROM positions")))
    t.start()
    assert entered.wait(5)
    closer = threading.Thread(target=lake.close)
    closer.start()
    time.sleep(0.1)
    assert closer.is_alive()  # blocked on the in-flight query
    release.set()
    t.join(5)
    closer.join(5)
    assert not closer.is_alive() and result == [[(3,)]]


def test_stress_queries_compaction_and_close(tmp_path):
    lake = Lake(tmp_path, duckdb_threads=2)
    for h in range(8):
        for _ in range(3):
            lake.write("positions", _rows(h))
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                (n,) = lake.query("SELECT count(*) FROM positions")[0]
                assert n % 3 == 0 and n > 0
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    def writer_compactor():
        try:
            for h in range(8, 14):
                lake.write("positions", _rows(h))
                lake.compact("positions", BASE + timedelta(hours=24))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()
    comp = threading.Thread(target=writer_compactor)
    comp.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not errors:
        lake.close()  # closes repeatedly under load; queries reconnect
        time.sleep(0.05)
    stop.set()
    comp.join(30)
    for t in readers:
        t.join(30)
    lake.close()
    assert not comp.is_alive() and not any(t.is_alive() for t in readers)
    assert errors == []
