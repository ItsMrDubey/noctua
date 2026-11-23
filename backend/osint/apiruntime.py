"""API-only runtime (`osint api`): the attribute surface the HTTP API reads from Runtime,
backed by read connections to the shared stores and the worker's published state file.
Starts no collectors or jobs and runs no maintenance (the worker owns those)."""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace
from typing import Any

from osint.config import Settings
from osint.core.scheduler import SourceHealth
from osint.state import WorkerState, state_path
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore

log = logging.getLogger(__name__)

# heartbeat older than this (or no state file) -> /health "worker_down"
WORKER_STALE_S = 10.0


class _Queue:
    def __init__(self, state: WorkerState) -> None:
        self._state = state

    def qsize(self) -> int:
        snap = self._state.get()
        return int(snap.ingestor.get("queue_depth", 0)) if snap else 0


class IngestorView:
    """``live`` / ``_lock`` / ``queue.qsize()`` / ``errors`` from the worker snapshot."""

    def __init__(self, state: WorkerState) -> None:
        self._state = state
        self._lock = threading.Lock()  # live is replaced on reload, never mutated
        self.queue = _Queue(state)

    def _ing(self) -> dict[str, Any]:
        snap = self._state.get()
        return snap.ingestor if snap else {}

    @property
    def live(self) -> dict:
        return self._state.live()

    @property
    def errors(self) -> int:
        return int(self._ing().get("errors", 0))

    @property
    def consecutive_flush_failures(self) -> int:
        return int(self._ing().get("consecutive_flush_failures", 0))

    @property
    def flushed(self) -> dict[str, str]:
        return dict(self._ing().get("flushed") or {})


class SchedulerView:
    def __init__(self, state: WorkerState) -> None:
        self._state = state

    @property
    def health(self) -> dict[str, SourceHealth]:
        snap = self._state.get()
        if snap is None:
            return {}
        return {name: SourceHealth(r.status, r.consecutive_failures, r.last_success,
                                   r.last_error, r.last_batch_size)
                for name, r in snap.scheduler.items()}


class MetricsView:
    def __init__(self, state: WorkerState) -> None:
        self._state = state

    def render_prometheus(self) -> str:
        snap = self._state.get()
        return snap.metrics if snap else ""


class StatusJob:
    """Stands in for a running job whose ``status()`` the worker published."""

    def __init__(self, status: dict) -> None:
        self._status = status

    def status(self) -> dict:
        return dict(self._status)


def _ns(d: dict | None) -> SimpleNamespace | None:
    return None if d is None else SimpleNamespace(**d)


class ApiRuntime:
    """Read-side runtime for the API process. Job attributes are views of the worker's
    published status (None when the worker does not run that job), so the endpoints'
    ``getattr(runtime, "<job>", None)`` code works unchanged."""

    def __init__(self, settings: Settings, state: WorkerState | None = None) -> None:
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = SqliteStore(settings.sqlite_path, migrate=False)
        self.lake = Lake(settings.lake_dir)
        self.state = state or WorkerState(state_path(settings.data_dir))
        self.ingestor = IngestorView(self.state)
        self.scheduler = SchedulerView(self.state)
        self.metrics = MetricsView(self.state)
        self.http = None  # no outbound fetches from the API process
        self._naval_job = None

    # ------------------------------------------------------------ worker state

    def _jobs(self) -> dict[str, Any]:
        snap = self.state.get()
        return snap.jobs if snap else {}

    def worker_info(self) -> dict:
        snap = self.state.get()
        age = None if snap is None else max(0.0, time.time() - snap.heartbeat)
        return {"alive": age is not None and age <= WORKER_STALE_S,
                "pid": snap.pid if snap else None,
                "heartbeat_age_s": None if age is None else round(age, 1),
                "booting": bool(snap and snap.booting),
                "boot_error": snap.boot_error if snap else None}

    @property
    def booting(self) -> bool:
        snap = self.state.get()
        return bool(snap and snap.booting)

    @property
    def boot_error(self) -> str | None:
        snap = self.state.get()
        return snap.boot_error if snap else None

    @property
    def ingestor_alive(self) -> bool:
        snap = self.state.get()
        return bool(snap and snap.ingestor.get("alive"))

    # ------------------------------------------------------------ job views

    @property
    def event_builder(self) -> SimpleNamespace | None:
        b = self._jobs().get("event_builder")
        if b is None:
            return None
        return SimpleNamespace(
            passes=b["passes"], last_error=b["last_error"], last_pass=_ns(b["last_pass"]),
            corroborator=SimpleNamespace(last=_ns(b.get("corroborator_last"))))

    @property
    def sensor_events(self) -> SimpleNamespace | None:
        return _ns(self._jobs().get("sensor_events"))

    @property
    def extraction_job(self) -> StatusJob | None:
        s = self._jobs().get("extraction")
        return None if s is None else StatusJob(s)

    @property
    def translate_job(self) -> StatusJob | None:
        s = self._jobs().get("translate")
        return None if s is None else StatusJob(s)

    @property
    def naval_job(self):
        """A CarrierJob that serves (and caches) hypotheses but never persists them; the
        worker's job owns naval.sqlite writes."""
        if self._naval_job is None:
            from osint.naval.job import CarrierJob, NavalConfig, NavalStore

            class ReadOnlyCarrierJob(CarrierJob):
                def compute(self, hours=None, *, now=None, persist=True):
                    return super().compute(hours, now=now, persist=False)

            cfg = NavalConfig.load(self.settings.config_dir / "naval.yaml")
            self._naval_job = ReadOnlyCarrierJob(
                self.lake, self.store, self.settings, cfg,
                NavalStore(self.settings.data_dir / "naval.sqlite"), http=None)
        return self._naval_job

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        log.info("api runtime: reading worker state from %s", self.state.path)

    async def stop(self) -> None:
        stores = [getattr(self, a, None) for a in (
            "extraction_store", "translation_store", "imagery_store")]
        if self._naval_job is not None:
            stores.append(self._naval_job.nstore)
        svc = getattr(self, "benchmark", None)
        if svc is not None:
            stores.append(svc.store)
        try:
            for s in stores:
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        log.exception("closing %s failed", type(s).__name__)
        finally:
            self.store.close()
            self.lake.close()
