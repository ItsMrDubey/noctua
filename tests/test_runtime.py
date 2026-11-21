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
