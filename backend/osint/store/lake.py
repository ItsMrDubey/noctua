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
from datetime import UTC, datetime, timedelta
from pathlib import Path

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


def _remove(path: Path) -> None:
    os.unlink(path)


def _compact_name(names: list[str]) -> str:
    digest = hashlib.sha1("\n".join(sorted(names)).encode("utf-8")).hexdigest()[:16]
    return f"{_COMPACT_PREFIX}{digest}.parquet"


def _hour_start(part_dir: Path) -> datetime | None:
    try:
        date = part_dir.parent.name.removeprefix("date=")
        hour = int(part_dir.name.removeprefix("hour="))
        return datetime.strptime(date, "%Y-%m-%d").replace(hour=hour, tzinfo=UTC)
    except ValueError:
        return None


class Lake:
    def __init__(
        self, root: Path, *, duckdb_memory_limit: str = "1GB", duckdb_threads: int = 4
    ) -> None:
        self.root = root
        self.duckdb_memory_limit = duckdb_memory_limit
        self.duckdb_threads = duckdb_threads
        root.mkdir(parents=True, exist_ok=True)
        # serialises file creation/removal: Lake.write (ingest flush) vs Lake.compact
        self.write_lock = threading.Lock()
        self._con_lock = threading.Lock()
        self._con: duckdb.DuckDBPyConnection | None = None

    # ------------------------------------------------------------------ writing

    def write(self, dataset: str, rows: list[dict]) -> int:
        schema = SCHEMAS[dataset]
        groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
        ts_field = PARTITION_FIELD.get(dataset, "ts")
        for r in rows:
            ts = r[ts_field].astimezone(UTC)  # partitions are always UTC
            groups[(ts.strftime("%Y-%m-%d"), ts.hour)].append(r)
        with self.write_lock:
            for (date, hour), group in groups.items():
                part_dir = self.root / dataset / f"date={date}" / f"hour={hour:02d}"
                part_dir.mkdir(parents=True, exist_ok=True)
                name = f"part-{time.time_ns()}-{uuid.uuid4().hex[:8]}.parquet"
                self._write_atomic(pa.Table.from_pylist(group, schema=schema), part_dir / name)
        return len(groups)

    @staticmethod
    def _write_atomic(table: pa.Table, dest: Path) -> None:
        tmp = dest.with_name(f"{dest.name}.tmp")
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

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        """A per-call cursor on the one long-lived connection (a cursor is never shared
        between threads; temp views are cursor-local, caches are database-wide)."""
        with self._con_lock:
            if self._con is None:
                self._con = self._connect()
            return self._con.cursor()

    def close(self) -> None:
        with self._con_lock:
            if self._con is not None:
                self._con.close()
                self._con = None

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
        for attempt in range(2):
            cur = self._cursor()
            bound: list[tuple[str, bool]] = []
            try:
                bound_sql, bound = self._bind(cur, sql, start, end)
                return cur.execute(bound_sql, params or []).fetchall()
            except duckdb.IOException:
                if attempt:  # a file vanished under us (compaction): relist once
                    raise
            finally:
                self._unbind(cur, bound)
                cur.close()
        raise AssertionError("unreachable")

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
            with self.write_lock:  # per partition, so a flush is never held up for long
                n += self._compact_partition(dataset, part_dir)
        return n

    def _compact_partition(self, dataset: str, part_dir: Path) -> int:
        for tmp in part_dir.glob("*.tmp"):  # leftovers of a crashed write (lock is held)
            tmp.unlink(missing_ok=True)
        files = sorted(part_dir.glob("*.parquet"))
        if len(files) <= 1:
            return 0
        names = [f.name for f in files]
        for f in files:  # crash recovery: a merged file already covers all the others
            if f.name.startswith(_COMPACT_PREFIX) and _compact_name(
                    [x for x in names if x != f.name]) == f.name:
                self._remove_inputs([x for x in files if x != f])
                return 1
        schema = SCHEMAS[dataset]
        table = pa.concat_tables(
            [pq.ParquetFile(f).read().select(schema.names).cast(schema) for f in files])
        table = table.group_by(schema.names, use_threads=False).aggregate([])
        table = table.select(schema.names).cast(schema).sort_by(
            PARTITION_FIELD.get(dataset, "ts"))
        self._write_atomic(table, part_dir / _compact_name(names))
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
