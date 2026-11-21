"""SQLite store for relational state: sources, cursors, tracks, quarantine."""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from osint.core.geo import utcnow
from osint.core.models import TrackInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  name TEXT PRIMARY KEY, domain TEXT NOT NULL, licence TEXT NOT NULL,
  commercial_ok INTEGER NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_state (
  name TEXT PRIMARY KEY, cursor TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tracks (
  track_id TEXT PRIMARY KEY, kind TEXT NOT NULL, ident TEXT NOT NULL,
  name TEXT, reg TEXT, type_code TEXT, operator TEXT, category TEXT,
  is_mil INTEGER NOT NULL DEFAULT 0, high_priority INTEGER NOT NULL DEFAULT 0,
  extra TEXT NOT NULL DEFAULT '{}', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS tracks_kind_mil ON tracks(kind, is_mil);
CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, record_type TEXT NOT NULL,
  reason TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
"""

_UPSERT_TRACK = """
INSERT INTO tracks (track_id, kind, ident, name, reg, type_code, operator, category,
                    is_mil, high_priority, extra, first_seen, last_seen)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(track_id) DO UPDATE SET
  name = COALESCE(excluded.name, tracks.name),
  reg = COALESCE(excluded.reg, tracks.reg),
  type_code = COALESCE(excluded.type_code, tracks.type_code),
  operator = COALESCE(excluded.operator, tracks.operator),
  category = COALESCE(excluded.category, tracks.category),
  is_mil = MAX(tracks.is_mil, excluded.is_mil),
  high_priority = MAX(tracks.high_priority, excluded.high_priority),
  extra = json_patch(tracks.extra, excluded.extra),
  last_seen = MAX(tracks.last_seen, excluded.last_seen)
"""


class SqliteStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def register_source(self, name: str, domain: str, licence: str, commercial_ok: bool) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO sources VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "domain=excluded.domain, licence=excluded.licence, "
                "commercial_ok=excluded.commercial_ok, updated_at=excluded.updated_at",
                (name, domain, licence, int(commercial_ok), utcnow().isoformat()),
            )

    def list_sources(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM sources ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def get_cursor(self, name: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT cursor FROM source_state WHERE name=?", (name,)).fetchone()
        return json.loads(row["cursor"]) if row else None

    def set_cursor(self, name: str, cursor: dict) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO source_state VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "cursor=excluded.cursor, updated_at=excluded.updated_at",
                (name, json.dumps(cursor), utcnow().isoformat()),
            )

    def upsert_tracks(self, tracks: list[TrackInfo], seen: datetime) -> None:
        if not tracks:
            return
        ts = seen.isoformat()
        rows = [
            (t.track_id, t.kind, t.ident, t.name, t.reg, t.type_code, t.operator, t.category,
             int(t.is_mil), int(t.high_priority), json.dumps(t.extra), ts, ts)
            for t in tracks
        ]
        with self._lock, self._db:
            self._db.executemany(_UPSERT_TRACK, rows)

    def get_track(self, track_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tracks WHERE track_id=?", (track_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["extra"] = json.loads(out["extra"])
        return out

    def get_tracks(self, track_ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(track_ids), 900):
            chunk = track_ids[i : i + 900]
            marks = ",".join("?" * len(chunk))
            with self._lock:
                rows = self._db.execute(
                    f"SELECT * FROM tracks WHERE track_id IN ({marks})", chunk
                ).fetchall()
            for r in rows:
                d = dict(r)
                d["extra"] = json.loads(d["extra"])
                out[d["track_id"]] = d
        return out

    def quarantine(self, source: str, record_type: str, reason: str, payload: dict) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO quarantine (source, record_type, reason, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                (source, record_type, reason, json.dumps(payload, default=str),
                 utcnow().isoformat()),
            )

    def quarantine_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()
