import asyncio
from datetime import timedelta

from osint.config import Settings
from osint.core.geo import utcnow
from osint.core.models import Batch, Position
from osint.runtime import Runtime
from tests.util import eventually


def _runtime(tmp_path):
    return Runtime(Settings(_env_file=None, data_dir=tmp_path), [])


async def test_maintenance_survives_prune_errors(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    runtime.maintenance_interval_s = 0.01
    calls = []

    def flaky_prune(older_than):
        calls.append(older_than)
        if len(calls) == 1:
            raise RuntimeError("prune blew up")

    monkeypatch.setattr(runtime.ingestor, "prune", flaky_prune)
    await runtime.start()
    try:
        await eventually(lambda: len(calls) >= 3)
    finally:
        await runtime.stop()


async def test_stop_flushes_and_closes_even_if_ingestor_task_died(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)

    async def dying_run():
        raise RuntimeError("ingestor crashed")

    monkeypatch.setattr(runtime.ingestor, "run", dying_run)
    await runtime.start()
    await eventually(lambda: not runtime.ingestor_alive)
    ts = utcnow() - timedelta(seconds=5)
    runtime.ingestor.process(Batch(source="t", positions=[
        Position(track_id="air:x", kind="air", ts=ts, lat=1, lon=2, source="t")]))
    await runtime.stop()
    assert runtime.lake.query("SELECT count(*) FROM positions") == [(1,)]
    assert runtime.http.is_closed
    assert all(t.done() for t in asyncio.all_tasks() if t.get_name() == "maintenance")


async def test_maintenance_prunes_off_the_event_loop(tmp_path, monkeypatch):
    import threading

    runtime = _runtime(tmp_path)
    runtime.maintenance_interval_s = 0.01
    threads = []

    def prune(older_than):
        threads.append(threading.current_thread())

    monkeypatch.setattr(runtime.ingestor, "prune", prune)
    await runtime.start()
    try:
        await eventually(lambda: len(threads) >= 1)
    finally:
        await runtime.stop()
    assert threads[0] is not threading.main_thread()


def test_prune_live_uses_per_kind_max_age(tmp_path):
    from osint.runtime import prune_live

    runtime = _runtime(tmp_path)
    now = utcnow()
    live = runtime.ingestor.live
    for tid, kind, age in (("air:new", "air", timedelta(minutes=5)),
                           ("air:old", "air", timedelta(minutes=45)),
                           ("sea:new", "sea", timedelta(hours=5)),
                           ("sea:old", "sea", timedelta(hours=7))):
        live[tid] = Position(track_id=tid, kind=kind, ts=now - age, lat=0, lon=0, source="t")
    assert prune_live(runtime.ingestor, now) == 2
    assert set(runtime.ingestor.live) == {"air:new", "sea:new"}
    runtime.store.close()
