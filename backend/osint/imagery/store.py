"""Imagery metadata in its own SQLite file (data_dir/imagery.sqlite)."""

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS imagery_assets (
  event_id TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('before','after')),
  collection TEXT, scene_id TEXT, acquired_at TEXT, cloud_pct REAL,
  path TEXT, bounds TEXT, change_score REAL,
  status TEXT NOT NULL, error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, next_try_at TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY (event_id, role));
"""
COLUMNS = ("event_id", "role", "collection", "scene_id", "acquired_at", "cloud_pct", "path",
           "bounds", "change_score", "status", "error", "attempts", "next_try_at", "updated_at")


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _out(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["bounds"] = json.loads(d["bounds"]) if d.get("bounds") else None
    return d


class ImageryStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def upsert(self, event_id: str, role: str, *, status: str, now: datetime,
               attempts: int = 0, next_try_at: datetime | None = None, error: str | None = None,
               collection: str | None = None, scene_id: str | None = None,
               acquired_at: datetime | None = None, cloud_pct: float | None = None,
               path: str | None = None, bounds: dict | None = None,
               change_score: float | None = None) -> None:
        row = (event_id, role, collection, scene_id,
               iso(acquired_at) if acquired_at else None, cloud_pct, path,
               json.dumps(bounds, separators=(",", ":")) if bounds else None, change_score,
               status, error, attempts, iso(next_try_at) if next_try_at else None, iso(now))
        with self._lock, self._db:
            self._db.execute(
                f"INSERT OR REPLACE INTO imagery_assets ({','.join(COLUMNS)}) "
                f"VALUES ({','.join('?' * len(COLUMNS))})", row)

    def set_change_score(self, event_id: str, score: float | None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE imagery_assets SET change_score=? WHERE event_id=?",
                             (score, event_id))

    def get_assets(self, event_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM imagery_assets WHERE event_id=? ORDER BY role", (event_id,)
            ).fetchall()
        return [_out(r) for r in rows]

    def assets_for(self, event_ids: list[str]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for i in range(0, len(event_ids), 500):
            chunk = event_ids[i:i + 500]
            with self._lock:
                rows = self._db.execute(
                    f"SELECT * FROM imagery_assets WHERE event_id IN ({','.join('?' * len(chunk))})",
                    chunk).fetchall()
            for r in rows:
                out.setdefault(r["event_id"], []).append(_out(r))
        return out
