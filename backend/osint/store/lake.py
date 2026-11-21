"""Append-only Parquet lake partitioned by hour, queried with DuckDB.

Layout: ``<root>/<dataset>/date=YYYY-MM-DD/hour=HH/*.parquet`` (UTC, by PARTITION_FIELD).
Each flush adds one small ``part-*`` file per (dataset, hour); ``Lake.compact`` later
merges every closed hour into a single ``compact-<hash>.parquet``. Queries given a time
window only open the files of the overlapping hours (partition pruning).
"""

import hashlib
import logging
import os
import re
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

import duckdb
import msgspec
import pyarrow as pa
import pyarrow.parquet as pq

from osint.core.geo import h3_cells
from osint.core.models import Document, Observation, Position, TriageResult

log = logging.getLogger(__name__)

_TS = pa.timestamp("us", tz="UTC")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_H3 = [("h3_3", pa.uint64()), ("h3_5", pa.uint64()), ("h3_7", pa.uint64())]

SCHEMAS: dict[str, pa.Schema] = {
    "positions": pa.schema([
        ("track_id", pa.string()), ("kind", pa.string()), ("ts", _TS),
        ("lat", pa.float64()), ("lon", pa.float64()), ("alt_ft", pa.int32()),
        ("speed_kts", pa.float32()), ("heading_deg", pa.float32()), ("source", pa.string()),
        *_H3,
    ]),
    "observations": pa.schema([
        ("id", pa.string()), ("version", pa.string()), ("type", pa.string()), ("ts", _TS),
        ("lat", pa.float64()), ("lon", pa.float64()), ("value", pa.float64()),
        ("source", pa.string()), ("attrs", pa.string()),
        *_H3,
    ]),
    # metadata + link only (no article text); triage output as columns; partitioned by
    # published_at. lat/lon/h3 are the best place and are null when no place was found.
    "documents": pa.schema([
        ("id", pa.string()), ("source", pa.string()), ("kind", pa.string()),
        ("origin_id", pa.string()), ("url", pa.string()), ("title", pa.string()),
        ("snippet", pa.string()), ("language", pa.string()),
        ("published_at", _TS), ("fetched_at", _TS), ("content_hash", pa.string()),
        ("attrs", pa.string()),
        ("relevance", pa.float32()), ("categories", pa.string()), ("places", pa.string()),
        ("place_name", pa.string()), ("place_precision", pa.string()),
        ("place_confidence", pa.float32()),
        ("lat", pa.float64()), ("lon", pa.float64()),
        *_H3,
    ]),
}

# column used for the date=/hour= partition of each dataset (default "ts")
PARTITION_FIELD: dict[str, str] = {"documents": "published_at"}


def _h3_fields(lat: float, lon: float) -> dict:
    c3, c5, c7 = h3_cells(lat, lon)
    return {"h3_3": c3, "h3_5": c5, "h3_7": c7}


def position_row(p: Position) -> dict:
    return {
        "track_id": p.track_id, "kind": p.kind, "ts": p.ts, "lat": p.lat, "lon": p.lon,
        "alt_ft": p.alt_ft, "speed_kts": p.speed_kts, "heading_deg": p.heading_deg,
        "source": p.source, **_h3_fields(p.lat, p.lon),
    }


def observation_row(o: Observation) -> dict:
    return {
        "id": o.id, "version": o.version, "type": o.type, "ts": o.ts, "lat": o.lat,
        "lon": o.lon, "value": o.value, "source": o.source,
        "attrs": msgspec.json.encode(o.attrs).decode(), **_h3_fields(o.lat, o.lon),
    }


def content_hash(title: str, snippet: str) -> str:
    text = " ".join(f"{title} {snippet}".casefold().split())
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def document_row(d: Document, t: TriageResult | None = None) -> dict:
    best = t.best_place if t is not None else None
    h3 = _h3_fields(best.lat, best.lon) if best is not None else {
        "h3_3": None, "h3_5": None, "h3_7": None}
    return {
        "id": d.id, "source": d.source, "kind": d.kind, "origin_id": d.origin_id,
        "url": d.url, "title": d.title, "snippet": d.snippet,
        "language": (t.language if t is not None and t.language else d.language),
        "published_at": d.published_at, "fetched_at": d.fetched_at,
        "content_hash": content_hash(d.title, d.snippet),
        "attrs": msgspec.json.encode(d.attrs).decode(),
        "relevance": t.relevance if t is not None else None,
        "categories": ",".join(t.categories) if t is not None else "",
        "places": msgspec.json.encode(t.places if t is not None else []).decode(),
        "place_name": best.name if best is not None else None,
        "place_precision": best.precision if best is not None else None,
        "place_confidence": best.confidence if best is not None else None,
        "lat": best.lat if best is not None else None,
        "lon": best.lon if best is not None else None,
        **h3,
    }


_DATASET_RE = re.compile(r"\b(" + "|".join(SCHEMAS) + r")\b")
# hive partition columns exposed by every dataset view (also on the empty placeholder)
# (hour stays VARCHAR: DuckDB does not autocast the zero-padded "05")
_HIVE_FIELDS = [("date", pa.date32()), ("hour", pa.string())]
_COMPACT_PREFIX = "compact-"
# close() waits this long for in-flight queries before closing the connection anyway
CLOSE_TIMEOUT_S = 30.0
_QUERY_ATTEMPTS = 4
DUCKDB_MEMORY_LIMIT = "512MB"
DUCKDB_THREADS = 2
LOCK_NAME = ".lock"
LOCK_TIMEOUT_S = 300.0
# a .tmp file older than this is a crashed writer's leftover (never one in progress)
STALE_TMP_S = 3600.0
# datasets whose rows are identified by ``id``: on merge the newest file's row wins (a
# rewritten row and its stale original never both survive a partial delete)
DEDUPE_BY_ID = frozenset({"documents"})


def _remove(path: Path) -> None:
    os.unlink(path)


class FileLock:
    """Exclusive inter-process lock on a file (``msvcrt.locking`` on Windows,
    ``fcntl.flock`` elsewhere), polled until ``timeout``. The OS releases it when the
    holder dies, so there are no stale locks. Not re-entrant; pair it with a thread lock."""

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_S) -> None:
        self.path = path
        self.timeout = timeout
        self._f = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")  # noqa: SIM115 - held until release()
        deadline = time.monotonic() + self.timeout
        delay = 0.005
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._f = f
                return
            except OSError:
                if time.monotonic() >= deadline:
                    f.close()
                    raise TimeoutError(f"lake lock {self.path} busy for {self.timeout:.0f}s")
                time.sleep(delay)
                delay = min(0.1, delay * 2)

    def release(self) -> None:
        f, self._f = self._f, None
        if f is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _tmp_path(dest: Path) -> Path:
    """Unique temp name next to ``dest`` (pid + random): concurrent writers in different
    processes never share or delete each other's temp files."""
    return dest.with_name(f"{dest.name}.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")


def _compact_name(names: list[str]) -> str:
    digest = hashlib.sha1("\n".join(sorted(names)).encode("utf-8")).hexdigest()[:16]
    return f"{_COMPACT_PREFIX}{digest}.parquet"


def merge_files(dataset: str, files: list[Path]) -> pa.Table:
    """Rows of ``files`` de-duplicated and sorted by the partition field.

    Datasets in DEDUPE_BY_ID keep one row per ``id``: the newest file's (mtime, then
    name), first occurrence within a file. Others drop exact duplicate rows only."""
    schema = SCHEMAS[dataset]
    by_id = dataset in DEDUPE_BY_ID

    def mtime(f: Path) -> float:
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0

    ordered = sorted(files, key=lambda f: (mtime(f), f.name), reverse=True)  # newest first
    tables = []
    offset = 0
    for f in ordered:
        t = pq.ParquetFile(f).read().select(schema.names).cast(schema)
        if by_id:
            t = t.append_column("__order", pa.array(range(offset, offset + t.num_rows),
                                                    pa.int64()))
            offset += t.num_rows
        tables.append(t)
    table = pa.concat_tables(tables)
    if by_id:
        # __order is the row's index in ``table``: keep the first row of each id
        first = table.group_by("id", use_threads=False).aggregate([("__order", "min")])
        table = table.take(first.column("__order_min")).drop_columns(["__order"])
    else:
        table = table.group_by(schema.names, use_threads=False).aggregate([])
    return table.select(schema.names).cast(schema).sort_by(PARTITION_FIELD.get(dataset, "ts"))


def _hour_start(part_dir: Path) -> datetime | None:
    try:
        date = part_dir.parent.name.removeprefix("date=")
        hour = int(part_dir.name.removeprefix("hour="))
        return datetime.strptime(date, "%Y-%m-%d").replace(hour=hour, tzinfo=UTC)
    except ValueError:
        return None


def _table_rows(table: pa.Table) -> list[dict]:
    """Rows as dicts with tz-aware UTC datetimes (``to_pylist`` needs tzdata/pytz, which
    Windows lacks)."""
    cols: dict[str, list] = {}
    for name in table.column_names:
        col = table.column(name)
        if pa.types.is_timestamp(col.type):
            us = col.cast(pa.int64()).to_pylist()
            cols[name] = [None if v is None else _EPOCH + timedelta(microseconds=v)
                          for v in us]
        else:
            cols[name] = col.to_pylist()
    return [dict(zip(cols, vals, strict=True)) for vals in zip(*cols.values(), strict=True)]


class Lake:
    def __init__(
        self, root: Path, *, duckdb_memory_limit: str | None = None,
        duckdb_threads: int | None = None,
    ) -> None:
        self.root = root
        # memory-constrained host: 512MB / 2 threads unless configured
        # (OSINT_DUCKDB_MEMORY_LIMIT, OSINT_DUCKDB_THREADS)
        self.duckdb_memory_limit = duckdb_memory_limit or os.environ.get(
            "OSINT_DUCKDB_MEMORY_LIMIT", DUCKDB_MEMORY_LIMIT)
        self.duckdb_threads = int(duckdb_threads or os.environ.get(
            "OSINT_DUCKDB_THREADS", DUCKDB_THREADS))
        root.mkdir(parents=True, exist_ok=True)
        # serialises file creation/removal: Lake.write (ingest flush) vs Lake.compact vs
        # Lake.rewrite_partition. In-process threads take this lock, then the dataset's
        # OS file lock (<root>/<dataset>/.lock) excludes other processes (CLI, backfill).
        self.write_lock = threading.RLock()
        self.lock_timeout = LOCK_TIMEOUT_S
        # guards the connection and the count of in-flight cursors; close() waits on it
        self._con_cond = threading.Condition()
        self._active = 0
        self._closing = 0  # close() calls draining in-flight cursors (new ones wait)
        self._con: duckdb.DuckDBPyConnection | None = None

    # ------------------------------------------------------------------ writing

    def write(self, dataset: str, rows: list[dict]) -> int:
        schema = SCHEMAS[dataset]
        groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
        ts_field = PARTITION_FIELD.get(dataset, "ts")
        for r in rows:
            ts = r[ts_field].astimezone(UTC)  # partitions are always UTC
            groups[(ts.strftime("%Y-%m-%d"), ts.hour)].append(r)
        with self.dataset_lock(dataset):
            for (date, hour), group in groups.items():
                part_dir = self.root / dataset / f"date={date}" / f"hour={hour:02d}"
                part_dir.mkdir(parents=True, exist_ok=True)
                name = f"part-{time.time_ns()}-{uuid.uuid4().hex[:8]}.parquet"
                self._write_atomic(pa.Table.from_pylist(group, schema=schema), part_dir / name)
        return len(groups)

    @contextmanager
    def dataset_lock(self, dataset: str) -> Iterator[None]:
        """Thread lock + inter-process file lock of one dataset (writes, compaction and
        partition rewrites of any process are mutually exclusive)."""
        with self.write_lock, FileLock(self.root / dataset / LOCK_NAME, self.lock_timeout):
            yield

    @staticmethod
    def _write_atomic(table: pa.Table, dest: Path) -> None:
        tmp = _tmp_path(dest)
        try:
            pq.write_table(table, tmp, compression="zstd")
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    # --------------------------------------------------------------- partitions

    def _partition_dirs(
        self, dataset: str, start: datetime | None = None, end: datetime | None = None
    ) -> Iterator[tuple[Path, datetime]]:
        """(hour dir, hour start) for every partition overlapping [start, end]."""
        ds_dir = self.root / dataset
        if not ds_dir.is_dir():
            return
        lo = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0) if start else None
        hi = end.astimezone(UTC) if end else None
        lo_day = lo.strftime("%Y-%m-%d") if lo else None
        hi_day = hi.strftime("%Y-%m-%d") if hi else None
        for date_dir in sorted(ds_dir.glob("date=*")):
            day = date_dir.name.removeprefix("date=")
            if (lo_day is not None and day < lo_day) or (hi_day is not None and day > hi_day):
                continue
            for hour_dir in sorted(date_dir.glob("hour=*")):
                hs = _hour_start(hour_dir)
                if hs is None or (lo is not None and hs < lo) or (hi is not None and hs > hi):
                    continue
                yield hour_dir, hs

    def partition_files(
        self, dataset: str, start: datetime | None = None, end: datetime | None = None
    ) -> list[Path]:
        """Parquet files of the hourly partitions overlapping [start, end] (None = open).

        The window is on the dataset's PARTITION_FIELD (``ts``; ``published_at`` for
        documents)."""
        return [f for d, _ in self._partition_dirs(dataset, start, end)
                for f in sorted(d.glob("*.parquet"))]

    # ------------------------------------------------------------------ queries

    def _connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(config={
            "memory_limit": self.duckdb_memory_limit, "threads": self.duckdb_threads})
        # DuckDB 2.x name for the old enable_object_cache: keep parquet footers cached
        # across queries on this long-lived connection
        con.execute("SET parquet_metadata_cache = true")
        return con

    @contextmanager
    def _cursor(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """A per-call cursor on the one long-lived connection (a cursor is never shared
        between threads; temp views are cursor-local, caches are database-wide). It is
        counted as in flight until closed, so close() never pulls the connection out from
        under a running query (DuckDB: "connection busy"/"already closed")."""
        with self._con_cond:
            self._con_cond.wait_for(lambda: not self._closing)
            if self._con is None:
                self._con = self._connect()
            cur = self._con.cursor()
            self._active += 1
        try:
            yield cur
        finally:
            try:
                cur.close()
            except duckdb.Error:
                pass
            with self._con_cond:
                self._active -= 1
                self._con_cond.notify_all()

    def close(self, timeout: float = CLOSE_TIMEOUT_S) -> None:
        """Close the connection once in-flight queries finish (bounded wait). Idempotent;
        a later query reconnects."""
        with self._con_cond:
            self._closing += 1
            try:
                if not self._con_cond.wait_for(lambda: self._active == 0, timeout):
                    log.warning("lake close: %d queries still running after %.0fs; closing",
                                self._active, timeout)
                if self._con is not None:
                    con, self._con = self._con, None
                    try:
                        con.close()
                    except duckdb.Error:
                        log.warning("lake close failed", exc_info=True)
            finally:
                self._closing -= 1
                self._con_cond.notify_all()

    def _bind(self, cur: duckdb.DuckDBPyConnection, sql: str,
              start: datetime | None, end: datetime | None) -> tuple[str, list[tuple[str, bool]]]:
        """Bind each dataset ``sql`` names to a per-call uniquely named view and return the
        rewritten SQL plus the bound names. Views live in the connection-wide catalog, so a
        shared name ("observations") makes concurrent queries conflict (write-write
        conflict on create) or read each other's file lists."""
        names: dict[str, str] = {}
        bound: list[tuple[str, bool]] = []
        for dataset in set(_DATASET_RE.findall(sql)):
            name = f"{dataset}_{uuid.uuid4().hex[:12]}"
            names[dataset] = name
            files = self.partition_files(dataset, start, end)
            if files:
                # schemas are fixed per dataset (no union_by_name: it reads every footer)
                rel = cur.read_parquet([f.as_posix() for f in files], hive_partitioning=True)
                rel.create_view(name, replace=True)
                bound.append((name, True))
            else:
                cur.register(name, pa.schema(
                    [*SCHEMAS[dataset], *_HIVE_FIELDS]).empty_table())
                bound.append((name, False))
        return _DATASET_RE.sub(lambda m: names[m.group(1)], sql), bound

    @staticmethod
    def _unbind(cur: duckdb.DuckDBPyConnection, bound: list[tuple[str, bool]]) -> None:
        for name, is_view in bound:
            try:
                if is_view:
                    cur.execute(f"DROP VIEW IF EXISTS {name}")
                else:
                    cur.unregister(name)
            except duckdb.Error:
                pass

    def query(self, sql: str, params: list | None = None, *,
              start: datetime | None = None, end: datetime | None = None) -> list[tuple]:
        """Run ``sql`` over the datasets it names.

        With ``start``/``end`` only the hourly partitions overlapping that window (on each
        dataset's PARTITION_FIELD) are read, so the SQL must not need rows outside it;
        without them the whole dataset is scanned (generic fallback)."""
        for attempt in range(_QUERY_ATTEMPTS):
            with self._cursor() as cur:
                bound: list[tuple[str, bool]] = []
                try:
                    bound_sql, bound = self._bind(cur, sql, start, end)
                    return cur.execute(bound_sql, params or []).fetchall()
                except duckdb.IOException:
                    # a file vanished under us (compaction merges partition by partition,
                    # so a whole-dataset listing can race several merges): relist
                    if attempt == _QUERY_ATTEMPTS - 1:
                        raise
                finally:
                    self._unbind(cur, bound)
            time.sleep(0.02 * (attempt + 1))
        raise AssertionError("unreachable")

    def iter_query(self, sql: str, params: list | None = None, *,
                   start: datetime | None = None, end: datetime | None = None,
                   batch: int = 10_000) -> Iterator[list[tuple]]:
        """Like ``query`` but yields the result in ``batch``-row lists (``fetchmany``), so
        a large result is never materialised at once. The cursor stays open until the
        generator is exhausted or closed; a vanished file mid-stream raises."""
        with self._cursor() as cur:
            bound: list[tuple[str, bool]] = []
            try:
                bound_sql, bound = self._bind(cur, sql, start, end)
                res = cur.execute(bound_sql, params or [])
                while rows := res.fetchmany(batch):
                    yield rows
            finally:
                self._unbind(cur, bound)

    # --------------------------------------------------------------- compaction

    def compact(self, dataset: str, older_than: datetime) -> int:
        """Merge each closed hourly partition (hour end <= ``older_than``) holding more
        than one file into one de-duplicated ``compact-<hash>.parquet``.

        Crash-safe: the merged file is written to .tmp and os.replace'd before the inputs
        are deleted; its name hashes the input names, so a rerun that finds it next to
        exactly its inputs just deletes them. Any other mix is merged again with exact
        duplicate removal, which cannot double rows. Returns partitions compacted."""
        n = 0
        for part_dir, hs in list(self._partition_dirs(dataset, None, older_than)):
            if hs + timedelta(hours=1) > older_than:
                continue
            # per partition, so a flush is never held up for long
            with self.dataset_lock(dataset):
                n += self._compact_partition(dataset, part_dir)
        return n

    @staticmethod
    def _remove_stale_tmp(part_dir: Path) -> None:
        """Delete crashed writers' .tmp leftovers. Only old ones: a fresh .tmp may belong
        to a writer in another process that does not use the lock (older code)."""
        cutoff = time.time() - STALE_TMP_S
        for tmp in part_dir.glob("*.tmp"):
            try:
                if tmp.stat().st_mtime < cutoff:
                    tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _compact_partition(self, dataset: str, part_dir: Path) -> int:
        self._remove_stale_tmp(part_dir)
        files = sorted(part_dir.glob("*.parquet"))
        if len(files) <= 1:
            return 0
        names = [f.name for f in files]
        for f in files:  # crash recovery: a merged file already covers all the others
            if f.name.startswith(_COMPACT_PREFIX) and _compact_name(
                    [x for x in names if x != f.name]) == f.name:
                self._remove_inputs([x for x in files if x != f])
                return 1
        self._write_atomic(merge_files(dataset, files), part_dir / _compact_name(names))
        self._remove_inputs(files)
        return 1

    @staticmethod
    def _remove_inputs(files: list[Path]) -> None:
        for f in files:
            try:
                _remove(f)
            except FileNotFoundError:
                pass
            except OSError as exc:  # e.g. Windows: held open by a concurrent reader
                log.warning("compaction could not remove %s (%s); retried next pass", f, exc)

    # ---------------------------------------------------------------- rewriting

    def partitions(self, dataset: str, start: datetime | None = None,
                   end: datetime | None = None) -> list[tuple[str, int]]:
        """(``YYYY-MM-DD``, hour) of every partition overlapping [start, end]."""
        return [(d.parent.name.removeprefix("date="), int(d.name.removeprefix("hour=")))
                for d, _ in self._partition_dirs(dataset, start, end)]

    def _hour_dir(self, dataset: str, date: str, hour: int) -> Path:
        return self.root / dataset / f"date={date}" / f"hour={hour:02d}"

    def read_partition(self, dataset: str, date: str, hour: int) -> tuple[list[dict], list[Path]]:
        """All rows of one hourly partition plus the files they came from (pass those to
        ``rewrite_partition`` as ``replaces``)."""
        schema = SCHEMAS[dataset]
        files = sorted(self._hour_dir(dataset, date, hour).glob("*.parquet"))
        rows: list[dict] = []
        for f in files:
            rows += _table_rows(pq.ParquetFile(f).read().select(schema.names).cast(schema))
        return rows, files

    def rewrite_partition(self, dataset: str, date: str, hour: int, rows: list[dict],
                          replaces: list[Path]) -> bool:
        """Atomically replace ``replaces`` (the files ``read_partition`` returned) with one
        file holding ``rows``. Files added since the read (an ingest flush) are kept.

        Holds the write lock only for the swap. The new file is written to .tmp and
        os.replace'd before the inputs are deleted; it is named like a compaction output of
        its inputs, so a crash in between is repaired by the next compaction. Returns False,
        changing nothing, if an input has vanished (compaction ran since the read): re-read
        and retry."""
        part_dir = self._hour_dir(dataset, date, hour)
        table = pa.Table.from_pylist(rows, schema=SCHEMAS[dataset])  # may raise: nothing done
        # every writer of this dataset (any process) holds the same lock, so the inputs
        # cannot vanish between this check and the swap
        with self.dataset_lock(dataset):
            if any(not f.exists() for f in replaces):
                return False
            out = _compact_name([f.name for f in replaces])
            self._write_atomic(table, part_dir / out)
            self._remove_inputs([f for f in replaces if f.name != out])
        return True

    # ------------------------------------------------------------------ indexes

    def recent_observation_index(self, since: datetime) -> dict[tuple[str, str], datetime]:
        """Latest observation ts per (id, version) seen since ``since``."""
        rows = self.query(
            "SELECT id, version, max(epoch_us(ts)) FROM observations WHERE ts >= ? "
            "GROUP BY id, version",
            [since], start=since,
        )
        return {(r[0], r[1]): _EPOCH + timedelta(microseconds=r[2]) for r in rows}

    def recent_document_index(
        self, since: datetime, max_age: timedelta = timedelta(hours=48)
    ) -> dict[str, datetime]:
        """fetched_at per document id fetched since ``since`` (dedupe warm start).

        Documents are partitioned by published_at and ingest skips any published more
        than ``max_age`` (DOC_MAX_AGE) before it is seen, so partitions older than
        ``since - max_age`` cannot hold a match and are not read."""
        rows = self.query(
            "SELECT id, max(epoch_us(fetched_at)) FROM documents WHERE fetched_at >= ? "
            "GROUP BY id",
            [since], start=since - max_age,
        )
        return {r[0]: _EPOCH + timedelta(microseconds=r[1]) for r in rows}
