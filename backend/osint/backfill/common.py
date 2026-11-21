"""Shared backfill plumbing: window planning, checkpoints, rate limits, HTTP, context."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any, Self

import httpx
import yaml

from osint.config import ROOT, Settings
from osint.core.geo import utcnow
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore

log = logging.getLogger("osint.backfill")

DEFAULT_CONFIG = ROOT / "config" / "backfill.yaml"
USER_AGENT = "osint-poc-backfill/0.1 (historical research data backfill; polite, rate-limited)"
CURSOR_PREFIX = "backfill:"
MAX_ERRORS_KEPT = 20
HEARTBEAT_STALE = timedelta(minutes=30)


# --------------------------------------------------------------------------- config


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        log.warning("backfill config %s missing; using defaults", path)
        return {}
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {str(k): (v or {}) for k, v in data.items() if isinstance(v, dict | None)}


# --------------------------------------------------------------------------- windows


@dataclass(frozen=True)
class Window:
    """Half-open UTC interval [start, end)."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"empty backfill window {self.start} .. {self.end}")

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


def _day_start(d: date) -> datetime:
    return datetime.combine(d, dtime(0), tzinfo=UTC)


def parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"invalid date {text!r} (expected YYYY-MM-DD)") from None


def resolve_window(*, days: int | None, since: date | None, until: date | None,
                   default_days: int, now: datetime | None = None,
                   default_since: date | None = None) -> Window:
    """--days N (ending now) or --since DATE [--until DATE] (inclusive day) -> Window.

    With neither, ``default_since`` (if given) else the last ``default_days`` days."""
    now = now or utcnow()
    if days is not None and (since is not None or until is not None):
        raise ValueError("use either --days or --since/--until, not both")
    end = min(now, _day_start(until) + timedelta(days=1)) if until is not None else now
    if days is not None:
        if days <= 0:
            raise ValueError("--days must be positive")
        start = end - timedelta(days=days)
    elif since is not None:
        start = _day_start(since)
    elif default_since is not None:
        start = _day_start(default_since)
    else:
        start = end - timedelta(days=default_days)
    return Window(start, end)


def day_units(w: Window) -> list[Window]:
    """The window cut at UTC midnights (first and last pieces may be partial)."""
    out: list[Window] = []
    cur = w.start
    while cur < w.end:
        nxt = min(_day_start(cur.date()) + timedelta(days=1), w.end)
        out.append(Window(cur, nxt))
        cur = nxt
    return out


def fixed_chunks(w: Window, size: timedelta) -> list[Window]:
    out: list[Window] = []
    cur = w.start
    while cur < w.end:
        nxt = min(cur + size, w.end)
        out.append(Window(cur, nxt))
        cur = nxt
    return out


def month_chunks(w: Window) -> list[Window]:
    """The window cut at calendar-month boundaries (UTC)."""
    out: list[Window] = []
    cur = w.start
    while cur < w.end:
        first = cur.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        nxt_month = (first + timedelta(days=32)).replace(day=1)
        nxt = min(nxt_month, w.end)
        out.append(Window(cur, nxt))
        cur = nxt
    return out


def split_half(w: Window) -> tuple[Window, Window]:
    mid = w.start + (w.end - w.start) / 2
    mid = mid.replace(microsecond=0)
    if mid <= w.start or mid >= w.end:
        raise ValueError("window too small to split")
    return Window(w.start, mid), Window(mid, w.end)


# ------------------------------------------------------------------------- secrets


def redact(text: object, *secrets: str | None) -> str:
    out = str(text)
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out


# --------------------------------------------------------------------- rate limits


class TokenBucket:
    """``capacity`` tokens refilled continuously over ``period_s`` (e.g. FIRMS: 5000 per
    600 s). ``acquire(cost)`` waits until ``cost`` tokens are available."""

    def __init__(self, capacity: float, period_s: float, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if capacity <= 0 or period_s <= 0:
            raise ValueError("capacity and period must be positive")
        self.capacity = float(capacity)
        self.rate = capacity / period_s  # tokens per second
        self.clock, self.sleep = clock, sleep
        self.tokens = float(capacity)
        self._last = clock()
        self._lock = asyncio.Lock()
        self.waited_s = 0.0

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
        self._last = now

    def wait_time(self, cost: float) -> float:
        self._refill()
        return 0.0 if self.tokens >= cost else (cost - self.tokens) / self.rate

    async def acquire(self, cost: float = 1.0) -> None:
        if cost > self.capacity:
            raise ValueError(f"cost {cost} exceeds bucket capacity {self.capacity}")
        async with self._lock:
            while True:
                wait = self.wait_time(cost)
                if wait <= 0:
                    self.tokens -= cost
                    return
                self.waited_s += wait
                await self.sleep(wait)


class Polite:
    """At most ``concurrency`` requests in flight, starts at least ``min_interval_s`` apart."""

    def __init__(self, concurrency: int = 1, min_interval_s: float = 0.0) -> None:
        self.sem = asyncio.Semaphore(max(1, concurrency))
        self.min_interval_s = min_interval_s
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def __aenter__(self) -> Self:
        await self.sem.acquire()
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.min_interval_s
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.sem.release()


class FetchError(RuntimeError):
    pass


async def get_with_retry(http: httpx.AsyncClient, url: str, *, secrets: Iterable[str] = (),
                         attempts: int = 4, backoff_s: float = 2.0,
                         timeout: float | httpx.Timeout = 120.0,
                         params: dict[str, Any] | None = None,
                         allow_status: Iterable[int] = (404,)) -> httpx.Response | None:
    """GET with retries on transport errors, 429 and 5xx. A status in ``allow_status``
    returns None (e.g. a missing GDELT file); other errors raise FetchError with any
    ``secrets`` redacted (the URL is never put in the message)."""
    allowed = set(allow_status)
    secrets = tuple(secrets)
    last = ""
    for attempt in range(attempts):
        try:
            r = await http.get(url, params=params, timeout=timeout)
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}"
        else:
            if r.status_code in allowed:
                return None
            if r.status_code < 400:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code != 429 and r.status_code < 500:
                raise FetchError(redact(f"{last}: {r.text[:200].strip()}", *secrets))
        if attempt < attempts - 1:
            await asyncio.sleep(backoff_s * 2 ** attempt)
    raise FetchError(redact(f"giving up after {attempts} attempts: {last}", *secrets))


# --------------------------------------------------------------------- checkpoints


class BackfillStore(SqliteStore):
    """SqliteStore with a long busy timeout: the live server writes the same database."""

    def __init__(self, path: Path, busy_timeout_ms: int = 60_000) -> None:
        super().__init__(path)
        with self._lock:
            self._db.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")


class Checkpoint:
    """Per-source progress in ``source_state`` (``backfill:<source>``): finished unit keys,
    cumulative rows, run state, recent errors. Saved after every unit, so a restart skips
    finished work."""

    def __init__(self, store: SqliteStore, source: str, *, dry_run: bool = False) -> None:
        self.store, self.source, self.dry_run = store, source, dry_run
        self.name = CURSOR_PREFIX + source
        self.state: dict[str, Any] = store.get_cursor(self.name) or {}
        self.done: set[str] = set(self.state.get("done") or [])
        self.run_rows: dict[str, int] = {}

    def is_done(self, key: str) -> bool:
        return key in self.done

    def _save(self) -> None:
        if self.dry_run:
            return
        self.state["done"] = sorted(self.done)
        self.state["updated_at"] = utcnow().isoformat()
        self.store.set_cursor(self.name, self.state)

    def start(self, window: Window | None, **extra: Any) -> None:
        self.state.update({
            "source": self.source, "state": "running", "pid": os.getpid(),
            "started_at": utcnow().isoformat(), "finished_at": None, "message": None,
            "window": window.as_dict() if window else None, "run_rows": {},
            "run_errors": 0, **extra})
        self._save()

    def add_rows(self, dataset: str, n: int) -> None:
        if n <= 0:
            return
        self.run_rows[dataset] = self.run_rows.get(dataset, 0) + n
        totals = self.state.setdefault("rows", {})
        totals[dataset] = int(totals.get(dataset, 0)) + n
        self.state["run_rows"] = dict(self.run_rows)

    def mark_done(self, key: str, **info: Any) -> None:
        self.done.add(key)
        self.state["last_checkpoint"] = key
        if info:
            self.state["last_info"] = info
        self._save()

    def heartbeat(self, **info: Any) -> None:
        self.state.update(info)
        self._save()

    def error(self, message: str) -> None:
        errs = list(self.state.get("errors") or [])
        errs.append({"at": utcnow().isoformat(), "message": message[:500]})
        self.state["errors"] = errs[-MAX_ERRORS_KEPT:]
        self.state["run_errors"] = int(self.state.get("run_errors") or 0) + 1
        self._save()

    def finish(self, state: str = "finished", message: str | None = None) -> None:
        self.state.update({"state": state, "message": message,
                           "finished_at": utcnow().isoformat()})
        self._save()


# ------------------------------------------------------------------------- context


@dataclass
class Summary:
    source: str
    state: str = "finished"
    units_total: int = 0
    units_done: int = 0
    units_skipped: int = 0  # already checkpointed
    rows: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    message: str | None = None
    seconds: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def add(self, dataset: str, n: int) -> None:
        self.rows[dataset] = self.rows.get(dataset, 0) + n

    def lines(self) -> list[str]:
        out = [(f"backfill {self.source}: {self.state} in {self.seconds:.1f}s; "
                f"units {self.units_done} done, {self.units_skipped} already done, "
                f"{self.units_total} planned")]
        for k, v in sorted(self.rows.items()):
            out.append(f"  {k}: {v:,}")
        for k, v in sorted(self.extra.items()):
            out.append(f"  {k}: {v}")
        if self.message:
            out.append(f"  note: {self.message}")
        for e in self.errors[:10]:
            out.append(f"  error: {e}")
        if len(self.errors) > 10:
            out.append(f"  ... {len(self.errors) - 10} more error(s)")
        return out


@dataclass
class BackfillContext:
    settings: Settings
    cfg: dict[str, Any]
    store: SqliteStore
    lake: Lake
    http: httpx.AsyncClient
    checkpoint: Checkpoint
    dry_run: bool = False
    tmp_dir: Path = field(default_factory=lambda: ROOT / "data" / "backfill" / "tmp")
    no_cluster: bool = False

    def opt(self, key: str, default: Any) -> Any:
        v = self.cfg.get(key)
        return default if v is None else v

    def write(self, dataset: str, rows: list[dict], summary: Summary) -> int:
        """Lake write unless dry-run; counts rows in the summary and checkpoint."""
        if not rows:
            return 0
        if not self.dry_run:
            self.lake.write(dataset, rows)
            self.checkpoint.add_rows(dataset, len(rows))
        summary.add(dataset, len(rows))
        return len(rows)


def new_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True,
                             timeout=httpx.Timeout(120, connect=30))
