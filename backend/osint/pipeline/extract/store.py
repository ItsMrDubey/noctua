"""Extraction results in their own SQLite file (data_dir/extraction.sqlite).

No article text is stored: only the structured result, short quotes and metadata.
"""

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS extractions (
  event_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, url TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('done','error')),
  is_real_event INTEGER, verdict TEXT CHECK (verdict IN ('supported','unsupported','mismatch')),
  reasons TEXT NOT NULL DEFAULT '[]', result TEXT, quotes TEXT NOT NULL DEFAULT '[]',
  geo TEXT, distance_km REAL, text_source TEXT, event_version INTEGER,
  cost_usd REAL NOT NULL DEFAULT 0, latency_s REAL, model TEXT, error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS extractions_updated ON extractions(updated_at);
CREATE TABLE IF NOT EXISTS llm_calls (
  ts REAL NOT NULL, event_id TEXT, ok INTEGER NOT NULL, cost_usd REAL NOT NULL DEFAULT 0,
  latency_s REAL, model TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS llm_calls_ts ON llm_calls(ts);
"""
_JSON = ("reasons", "result", "quotes", "geo")


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _out(row: sqlite3.Row) -> dict:
    d = dict(row)
    for c in _JSON:
        d[c] = json.loads(d[c]) if d.get(c) else None
    d["is_real_event"] = None if d["is_real_event"] is None else bool(d["is_real_event"])
    return d


def _dump(v) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, separators=(",", ":"))


class ExtractionStore:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        """``read_only``: URI ``mode=ro`` connection, no schema creation (report-only runs
        against a live database, e.g. ``migrate_countries --dry-run``)."""
        self._lock = threading.Lock()
        if read_only:
            self._db = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True,
                                       check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ results

    def save(self, event_id: str, *, doc_id: str, url: str, status: str, now: datetime,
             is_real_event: bool | None = None, verdict: str | None = None,
             reasons: list[str] | None = None, result: dict | None = None,
             geo: dict | None = None, distance_km: float | None = None,
             text_source: str | None = None, event_version: int | None = None,
             cost_usd: float = 0.0, latency_s: float | None = None, model: str | None = None,
             error: str | None = None, attempts: int = 1) -> None:
        quotes = (result or {}).get("quotes") or []
        with self._lock, self._db:
            prev = self._db.execute("SELECT created_at, cost_usd FROM extractions "
                                    "WHERE event_id=?", (event_id,)).fetchone()
            created = prev["created_at"] if prev else iso(now)
            total_cost = (prev["cost_usd"] if prev else 0.0) + cost_usd
            self._db.execute(
                "INSERT OR REPLACE INTO extractions (event_id, doc_id, url, status, "
                "is_real_event, verdict, reasons, result, quotes, geo, distance_km, text_source,"
                " event_version, cost_usd, latency_s, model, error, attempts, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, doc_id, url, status,
                 None if is_real_event is None else int(is_real_event), verdict,
                 _dump(reasons or []), _dump(result), _dump(quotes), _dump(geo), distance_km,
                 text_source, event_version, total_cost, latency_s, model,
                 error[:500] if error else None, attempts, created, iso(now)))

    def get(self, event_id: str) -> dict | None:
        """Latest extraction for an event (result JSON, quotes, verdict) or None."""
        with self._lock:
            row = self._db.execute("SELECT * FROM extractions WHERE event_id=?",
                                   (event_id,)).fetchone()
        return _out(row) if row else None

    def get_many(self, event_ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(event_ids), 500):
            chunk = event_ids[i:i + 500]
            with self._lock:
                rows = self._db.execute(
                    f"SELECT * FROM extractions WHERE event_id IN ({','.join('?' * len(chunk))})",
                    chunk).fetchall()
            out.update({r["event_id"]: _out(r) for r in rows})
        return out

    def verdicts_since(self, since: datetime) -> dict[str, str]:
        """{event_id: verdict} for successful extractions updated since ``since``."""
        with self._lock:
            rows = self._db.execute(
                "SELECT event_id, verdict FROM extractions WHERE status='done' AND "
                "updated_at >= ?", (iso(since),)).fetchall()
        return {r[0]: r[1] for r in rows}

    def verdict_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT COALESCE(verdict, status), COUNT(*) FROM extractions GROUP BY 1"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    # ------------------------------------------------------------------ call log

    def log_call(self, ts: float, *, event_id: str | None, ok: bool, cost_usd: float = 0.0,
                 latency_s: float | None = None, model: str | None = None,
                 error: str | None = None) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?)",
                             (ts, event_id, int(ok), cost_usd, latency_s, model,
                              error[:300] if error else None))

    def call_times_since(self, since_ts: float) -> list[float]:
        with self._lock:
            rows = self._db.execute("SELECT ts FROM llm_calls WHERE ts >= ? ORDER BY ts",
                                    (since_ts,)).fetchall()
        return [r[0] for r in rows]

    def cost_since(self, since_ts: float) -> float:
        with self._lock:
            row = self._db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls "
                                   "WHERE ts >= ?", (since_ts,)).fetchone()
        return float(row[0])

    def last_errors(self, limit: int = 5) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, event_id, error FROM llm_calls WHERE ok=0 ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        return [{"at": iso(datetime.fromtimestamp(r[0], UTC)), "event_id": r[1],
                 "error": r[2]} for r in rows]
