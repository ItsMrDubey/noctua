"""data_dir/naval.sqlite: hypothesis identity + history, and small JSON state (USNI cache,
traffic baseline)."""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from osint.core.geo import haversine_km, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hypotheses (
  id TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_updated TEXT NOT NULL,
  lat REAL NOT NULL, lon REAL NOT NULL, radius_km REAL NOT NULL, confidence REAL NOT NULL,
  kinds TEXT NOT NULL DEFAULT '[]', candidate_names TEXT NOT NULL DEFAULT '[]',
  evidence TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS hypotheses_updated ON hypotheses(last_updated);
CREATE TABLE IF NOT EXISTS hypothesis_history (
  id TEXT NOT NULL, ts TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
  radius_km REAL NOT NULL, confidence REAL NOT NULL, kinds TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (id, ts));
"""


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class NavalStore:
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
                (key, json.dumps(value), _iso(utcnow())))

    # ------------------------------------------------------------ hypotheses

    def recent(self, since: datetime) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, first_seen, last_updated, lat, lon FROM hypotheses "
                "WHERE last_updated >= ?", (_iso(since),)).fetchall()
        return [dict(r) for r in rows]

    def match(self, lat: float, lon: float, candidates: list[dict], max_km: float,
              taken: set[str]) -> dict | None:
        best, best_d = None, max_km
        for c in candidates:
            if c["id"] in taken:
                continue
            d = haversine_km(lat, lon, c["lat"], c["lon"])
            if d <= best_d:
                best, best_d = c, d
        return best

    def save(self, hyps: list[dict], now: datetime) -> None:
        ts = _iso(now)
        with self._lock, self._db:
            for h in hyps:
                self._db.execute(
                    "INSERT INTO hypotheses VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE "
                    "SET last_updated=excluded.last_updated, lat=excluded.lat, lon=excluded.lon, "
                    "radius_km=excluded.radius_km, confidence=excluded.confidence, "
                    "kinds=excluded.kinds, candidate_names=excluded.candidate_names, "
                    "evidence=excluded.evidence",
                    (h["id"], h["first_seen"], ts, h["lat"], h["lon"], h["radius_km"],
                     h["confidence"], json.dumps(h["kinds"]), json.dumps(h["candidate_names"]),
                     json.dumps(h["evidence"])))
                self._db.execute(
                    "INSERT OR REPLACE INTO hypothesis_history VALUES (?,?,?,?,?,?,?)",
                    (h["id"], ts, h["lat"], h["lon"], h["radius_km"], h["confidence"],
                     json.dumps(h["kinds"])))

    def get(self, hyp_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM hypotheses WHERE id=?", (hyp_id,)).fetchone()
            hist = self._db.execute(
                "SELECT ts, lat, lon, radius_km, confidence, kinds FROM hypothesis_history "
                "WHERE id=? ORDER BY ts", (hyp_id,)).fetchall()
        if row is None:
            return None
        d = dict(row)
        for k in ("kinds", "candidate_names", "evidence"):
            d[k] = json.loads(d[k])
        d["history"] = [{**dict(r), "kinds": json.loads(r["kinds"])} for r in hist]
        return d

    def prune_history(self, before: datetime) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM hypothesis_history WHERE ts < ?", (_iso(before),))
            self._db.execute("DELETE FROM hypotheses WHERE last_updated < ?", (_iso(before),))
