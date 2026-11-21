"""Append-only Parquet lake partitioned by hour, queried with DuckDB."""

import hashlib
import os
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import msgspec
import pyarrow as pa
import pyarrow.parquet as pq

from osint.core.geo import h3_cells
from osint.core.models import Document, Observation, Position, TriageResult

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


class Lake:
    def __init__(
        self, root: Path, *, duckdb_memory_limit: str = "1GB", duckdb_threads: int = 4
    ) -> None:
        self.root = root
        self.duckdb_memory_limit = duckdb_memory_limit
        self.duckdb_threads = duckdb_threads
        root.mkdir(parents=True, exist_ok=True)

    def write(self, dataset: str, rows: list[dict]) -> int:
        schema = SCHEMAS[dataset]
        groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
        ts_field = PARTITION_FIELD.get(dataset, "ts")
        for r in rows:
            ts = r[ts_field].astimezone(UTC)  # partitions are always UTC
            groups[(ts.strftime("%Y-%m-%d"), ts.hour)].append(r)
        for (date, hour), group in groups.items():
            part_dir = self.root / dataset / f"date={date}" / f"hour={hour:02d}"
            part_dir.mkdir(parents=True, exist_ok=True)
            name = f"part-{time.time_ns()}-{uuid.uuid4().hex[:8]}.parquet"
            tmp = part_dir / f"{name}.tmp"
            try:
                pq.write_table(
                    pa.Table.from_pylist(group, schema=schema), tmp, compression="zstd"
                )
                os.replace(tmp, part_dir / name)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        return len(groups)

    def _connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(
            config={"memory_limit": self.duckdb_memory_limit, "threads": self.duckdb_threads}
        )
        for dataset, schema in SCHEMAS.items():
            ds_dir = self.root / dataset
            if ds_dir.is_dir() and any(ds_dir.rglob("*.parquet")):
                pattern = (ds_dir / "**" / "*.parquet").as_posix().replace("'", "''")
                con.execute(
                    f"CREATE VIEW {dataset} AS SELECT * FROM read_parquet('{pattern}', "
                    "hive_partitioning=true, union_by_name=true)"
                )
            else:
                con.register(dataset, schema.empty_table())
        return con

    def query(self, sql: str, params: list | None = None) -> list[tuple]:
        con = self._connect()
        try:
            return con.execute(sql, params or []).fetchall()
        finally:
            con.close()

    def recent_observation_index(self, since: datetime) -> dict[tuple[str, str], datetime]:
        """Latest observation ts per (id, version) seen since ``since``."""
        rows = self.query(
            "SELECT id, version, max(epoch_us(ts)) FROM observations WHERE ts >= ? "
            "GROUP BY id, version",
            [since],
        )
        return {(r[0], r[1]): _EPOCH + timedelta(microseconds=r[2]) for r in rows}

    def recent_document_index(self, since: datetime) -> dict[str, datetime]:
        """fetched_at per document id fetched since ``since`` (dedupe warm start)."""
        rows = self.query(
            "SELECT id, max(epoch_us(fetched_at)) FROM documents WHERE fetched_at >= ? "
            "GROUP BY id",
            [since],
        )
        return {r[0]: _EPOCH + timedelta(microseconds=r[1]) for r in rows}
