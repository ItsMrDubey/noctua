"""data_dir/fire_scores.sqlite: one row per scored FIRMS hotspot (written by the worker's
FireScoreJob, read by the fires layer API), plus small JSON job state in ``meta``.

``ts`` is the detection time (UTC ISO, lexically sortable), so the API joins a layer
window with one indexed range query. ``fp`` is the config fingerprint the row was scored
with (a config edit rescores the window)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from osint.core.geo import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS fire_scores (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
  score INTEGER NOT NULL, reasons TEXT NOT NULL DEFAULT '', features TEXT NOT NULL DEFAULT '{}',
  fp TEXT NOT NULL, computed_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS fire_scores_ts ON fire_scores(ts);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
"""
STORE_NAME = "fire_scores.sqlite"


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


class FireScoreStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key: str, value: dict) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO meta VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value, default=str), iso(utcnow())))

    # ---------------------------------------------------------------- scores

    def save(self, rows: list[dict], fp: str, now: datetime | None = None) -> int:
        """Upsert rows {id, ts (datetime), lat, lon, score, reasons, features}."""
        at = iso(now or utcnow())
        data = [(r["id"], iso(r["ts"]), r["lat"], r["lon"], int(r["score"]),
                 ",".join(r["reasons"]), json.dumps(r["features"], separators=(",", ":")),
                 fp, at) for r in rows]
        with self._lock, self._db:
            self._db.executemany(
                "INSERT INTO fire_scores VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE "
                "SET ts=excluded.ts, lat=excluded.lat, lon=excluded.lon, score=excluded.score, "
                "reasons=excluded.reasons, features=excluded.features, fp=excluded.fp, "
                "computed_at=excluded.computed_at", data)
        return len(data)

    def fingerprints(self, start: datetime, end: datetime) -> dict[str, str]:
        """id -> config fingerprint of the rows detected in [start, end]."""
        with self._lock:
            rows = self._db.execute("SELECT id, fp FROM fire_scores WHERE ts >= ? AND ts <= ?",
                                    (iso(start), iso(end))).fetchall()
        return dict(rows)

    def between(self, start: datetime, end: datetime) -> dict[str, tuple[int, str]]:
        """id -> (score, comma-separated reasons) of the rows detected in [start, end]."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, score, reasons FROM fire_scores WHERE ts >= ? AND ts <= ?",
                (iso(start), iso(end))).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    def get(self, fire_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id, ts, lat, lon, score, reasons, features, computed_at "
                "FROM fire_scores WHERE id = ?", (fire_id,)).fetchone()
        if row is None:
            return None
        return {"id": row[0], "ts": row[1], "lat": row[2], "lon": row[3], "score": row[4],
                "reasons": [r for r in row[5].split(",") if r],
                "features": json.loads(row[6] or "{}"), "computed_at": row[7]}

    def stats(self, start: datetime, end: datetime, threshold: int) -> dict:
        with self._lock:
            n, hi = self._db.execute(
                "SELECT count(*), coalesce(sum(score >= ?), 0) FROM fire_scores "
                "WHERE ts >= ? AND ts <= ?", (threshold, iso(start), iso(end))).fetchone()
        return {"scored": n, "war_likely": hi, "threshold": threshold}

    def prune(self, before: datetime) -> int:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM fire_scores WHERE ts < ?",
                                    (iso(before),)).rowcount


def get_score_store(runtime) -> FireScoreStore:
    """The runtime's score store, created on first use (shared by the job and the API)."""
    store = getattr(runtime, "fire_score_store", None)
    if store is None:
        store = FireScoreStore(runtime.settings.data_dir / STORE_NAME)
        runtime.fire_score_store = store
    return store
