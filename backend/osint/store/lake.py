"""Append-only Parquet lake partitioned by hour, queried with DuckDB."""

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
from osint.core.models import Observation, Position

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
}


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


class Lake:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def write(self, dataset: str, rows: list[dict]) -> int:
        schema = SCHEMAS[dataset]
        groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
        for r in rows:
            groups[(r["ts"].strftime("%Y-%m-%d"), r["ts"].hour)].append(r)
        for (date, hour), group in groups.items():
            part_dir = self.root / dataset / f"date={date}" / f"hour={hour:02d}"
            part_dir.mkdir(parents=True, exist_ok=True)
            name = f"part-{time.time_ns()}-{uuid.uuid4().hex[:8]}.parquet"
            tmp = part_dir / f"{name}.tmp"
            pq.write_table(pa.Table.from_pylist(group, schema=schema), tmp, compression="zstd")
            os.replace(tmp, part_dir / name)
        return len(groups)

    def _connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect()
        for dataset, schema in SCHEMAS.items():
            ds_dir = self.root / dataset
            if ds_dir.is_dir() and any(ds_dir.rglob("*.parquet")):
                pattern = (ds_dir / "**" / "*.parquet").as_posix()
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
