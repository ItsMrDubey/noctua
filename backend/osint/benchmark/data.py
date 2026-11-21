"""Read-only access to the platform's stores for the benchmark.

The events database is opened read-only per call (no contention with the writer's lock);
GDELT rows are aggregated in DuckDB over the hourly ``documents`` partitions of the window
only, on a short-lived connection with a memory cap, so nothing is ever loaded wholesale
into Python."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa

from osint.benchmark.config import BenchmarkConfig
from osint.store.lake import SCHEMAS, Lake
from osint.store.sqlite import connect

_DIGITS = re.compile(r"^\d{1,4}$")
_LAKE_ATTEMPTS = 3


def parse_ts(raw: str | None) -> datetime | None:
    """Events-table ISO text -> aware UTC datetime (None when empty/invalid)."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def tracked_sql(roots: frozenset[str] | set[str], codes: tuple[str, ...] | list[str],
                root_expr: str, code_expr: str) -> str:
    """SQL predicate: CAMEO root in ``roots`` or event code starting with one of ``codes``
    (values are validated digit strings, so inlining them is safe)."""
    parts = []
    rs = sorted(r for r in roots if _DIGITS.match(r))
    if rs:
        parts.append(f"{root_expr} IN ({', '.join(repr(r) for r in rs)})")
    for c in codes:
        if _DIGITS.match(c):
            parts.append(f"starts_with(COALESCE({code_expr}, ''), '{c}')")
    return "(" + " OR ".join(parts) + ")" if parts else "FALSE"


@dataclass
class Sources:
    sqlite_path: Path
    extraction_path: Path | None
    lake: Lake | None
    cfg: BenchmarkConfig
    tracked_roots: frozenset[str] = frozenset({"13", "14", "15", "16", "17", "18", "19", "20"})
    tracked_codes: tuple[str, ...] = ()
    multi_source_min_domains: int = 2

    @classmethod
    def from_settings(cls, settings, lake: Lake | None,
                      cfg: BenchmarkConfig | None = None) -> Sources:
        from osint.pipeline.events import EventsConfig

        cfg_dir = Path(settings.config_dir)
        ev = EventsConfig.from_file(cfg_dir / "events.yaml")
        return cls(sqlite_path=Path(settings.sqlite_path),
                   extraction_path=Path(settings.data_dir) / "extraction.sqlite",
                   lake=lake,
                   cfg=cfg or BenchmarkConfig.from_file(cfg_dir / "benchmark.yaml"),
                   tracked_roots=ev.gdelt_tracked_roots, tracked_codes=ev.gdelt_tracked_codes,
                   multi_source_min_domains=ev.multi_source_min_domains)

    # ------------------------------------------------------------------- sqlite

    @staticmethod
    @contextmanager
    def _ro(path: Path) -> Iterator[sqlite3.Connection | None]:
        if not path.is_file():
            yield None
            return
        # read-only, autocommit: each statement holds its read snapshot only while it
        # runs, so the benchmark never pins the WAL (checkpoint starvation)
        db = connect(path, read_only=True)
        db.isolation_level = None
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def events_db(self) -> Iterator[sqlite3.Connection | None]:
        with self._ro(self.sqlite_path) as db:
            yield db

    @contextmanager
    def extraction_db(self) -> Iterator[sqlite3.Connection | None]:
        if self.extraction_path is None:
            yield None
            return
        with self._ro(self.extraction_path) as db:
            yield db

    # --------------------------------------------------------------------- lake

    def tracked(self, root_expr: str, code_expr: str) -> str:
        return tracked_sql(self.tracked_roots, self.tracked_codes, root_expr, code_expr)

    def lake_query(self, sql: str, params: list | None = None, *, start: datetime,
                   end: datetime, tables: dict[str, pa.Table] | None = None) -> list[tuple]:
        """Run ``sql`` with a ``documents`` view over the partitions of [start, end] and
        any extra Arrow ``tables`` registered under their names. Timestamps must be
        returned as epoch numbers (the host time zone may be unknown to Python)."""
        for attempt in range(_LAKE_ATTEMPTS):
            files = (self.lake.partition_files("documents", start, end)
                     if self.lake is not None else [])
            con = duckdb.connect(config={"memory_limit": self.cfg.duckdb_memory_limit,
                                         "threads": self.cfg.duckdb_threads})
            try:
                for name, table in (tables or {}).items():
                    con.register(name, table)
                if files:
                    con.read_parquet([f.as_posix() for f in files],
                                     hive_partitioning=False).create_view("documents")
                else:
                    con.register("documents", SCHEMAS["documents"].empty_table())
                return con.execute(sql, params or []).fetchall()
            except duckdb.IOException:
                # a partition was compacted under us: relist and retry
                if attempt == _LAKE_ATTEMPTS - 1:
                    raise
            finally:
                con.close()
        raise AssertionError("unreachable")
