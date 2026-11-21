"""Benchmark state in its own SQLite file (data_dir/benchmark.sqlite): hourly scorecard
snapshots (flattened numeric metrics plus the full JSON) and human gold-set labels."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  ts TEXT NOT NULL, window TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL,
  PRIMARY KEY (window, metric, ts));
CREATE INDEX IF NOT EXISTS snapshots_ts ON snapshots(ts);
CREATE TABLE IF NOT EXISTS scorecards (
  ts TEXT NOT NULL, window TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (window, ts));
CREATE TABLE IF NOT EXISTS labels (
  event_id TEXT PRIMARY KEY, correct_event INTEGER NOT NULL, correct_location INTEGER NOT NULL,
  notes TEXT NOT NULL DEFAULT '', labelled_at TEXT NOT NULL);
"""


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def flatten(obj: object, prefix: str = "") -> dict[str, float]:
    """Numeric leaves of a nested dict as {"a.b.c": value} (bools and None skipped)."""
    out: dict[str, float] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, int | float) and not isinstance(obj, bool):
        out[prefix] = float(obj)
    return out


class BenchmarkStore:
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

    # ---------------------------------------------------------------- snapshots

    def save_snapshot(self, window: str, scorecard: dict, ts: datetime) -> int:
        """Persist the scorecard's numeric metrics (``metrics`` subtree, flattened) and
        its full JSON under ``ts`` (hour-truncated, so a re-run in the same hour
        overwrites). Returns the number of metric values stored."""
        stamp = iso(ts.replace(minute=0, second=0, microsecond=0))
        flat = flatten(scorecard.get("metrics") or {})
        with self._lock, self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?)",
                [(stamp, window, k, v) for k, v in flat.items()])
            self._db.execute("INSERT OR REPLACE INTO scorecards VALUES (?,?,?)",
                             (stamp, window, json.dumps(scorecard, default=str)))
        return len(flat)

    def history(self, window: str, since: datetime,
                metric: str | None = None) -> dict[str, list[dict]]:
        """{metric: [{ts, value}, ...]} oldest first (one metric or all)."""
        sql = "SELECT metric, ts, value FROM snapshots WHERE window=? AND ts >= ?"
        args: list = [window, iso(since)]
        if metric:
            sql += " AND metric=?"
            args.append(metric)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY metric, ts", args).fetchall()
        out: dict[str, list[dict]] = {}
        for m, ts, v in rows:
            out.setdefault(m, []).append({"ts": ts, "value": v})
        return out

    def metric_names(self, window: str) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT DISTINCT metric FROM snapshots WHERE window=? "
                                    "ORDER BY metric", (window,)).fetchall()
        return [r[0] for r in rows]

    def latest_scorecard(self, window: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT body FROM scorecards WHERE window=? "
                                   "ORDER BY ts DESC LIMIT 1", (window,)).fetchone()
        return json.loads(row[0]) if row else None

    def prune(self, older_than: datetime) -> int:
        cut = iso(older_than)
        with self._lock, self._db:
            n = self._db.execute("DELETE FROM snapshots WHERE ts < ?", (cut,)).rowcount
            self._db.execute("DELETE FROM scorecards WHERE ts < ?", (cut,))
        return n

    # ------------------------------------------------------------------- labels

    def save_label(self, event_id: str, correct_event: bool, correct_location: bool,
                   notes: str = "", now: datetime | None = None) -> dict:
        row = {"event_id": event_id, "correct_event": bool(correct_event),
               "correct_location": bool(correct_location), "notes": notes or "",
               "labelled_at": iso(now or datetime.now(UTC))}
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO labels VALUES (?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET "
                "correct_event=excluded.correct_event, "
                "correct_location=excluded.correct_location, notes=excluded.notes, "
                "labelled_at=excluded.labelled_at",
                (event_id, int(row["correct_event"]), int(row["correct_location"]),
                 row["notes"], row["labelled_at"]))
        return row

    def labels(self, event_ids: list[str] | None = None) -> dict[str, dict]:
        with self._lock:
            if event_ids is None:
                rows = self._db.execute("SELECT * FROM labels").fetchall()
            else:
                rows = []
                for i in range(0, len(event_ids), 500):
                    chunk = event_ids[i:i + 500]
                    rows += self._db.execute(
                        f"SELECT * FROM labels WHERE event_id IN "
                        f"({','.join('?' * len(chunk))})", chunk).fetchall()
        out = {}
        for r in rows:
            d = dict(r)
            d["correct_event"] = bool(d["correct_event"])
            d["correct_location"] = bool(d["correct_location"])
            out[d["event_id"]] = d
        return out


def retention_cutoff(now: datetime, days: float) -> datetime:
    return now - timedelta(days=days)
