"""Translation cache in its own SQLite file (data_dir/translations.sqlite).

``translations``: one row per (language, text), key = sha1(lang + "\\n" + text); status
pending | done | failed | passthrough (English is stored with text_en = text, never sent to
the LLM). ``refs``: which text currently stands for an event/document field
(``e:<event_id>:title|summary``, ``d:<doc_id>:title|snippet``) so the API can look English
versions up without the lake. ``calls``: one row per LLM call (caps, cost, latency).
"""

import hashlib
import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
  key TEXT PRIMARY KEY, src_lang TEXT NOT NULL, text TEXT NOT NULL, text_en TEXT,
  model TEXT, status TEXT NOT NULL CHECK (status IN ('pending','done','failed','passthrough')),
  prio INTEGER NOT NULL DEFAULT 9, sort_ts TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS translations_queue ON translations(status, prio, sort_ts);
CREATE TABLE IF NOT EXISTS refs (
  ref TEXT PRIMARY KEY, key TEXT NOT NULL, lang TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calls (
  ts REAL NOT NULL, ok INTEGER NOT NULL, n_items INTEGER NOT NULL, n_ok INTEGER NOT NULL,
  cost_usd REAL NOT NULL DEFAULT 0, latency_s REAL, model TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
"""
_WS = re.compile(r"\s+")


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def norm_lang(lang: str | None) -> str:
    return (lang or "").strip().split("-")[0].split("_")[0].lower()


def clean(text: str | None, max_chars: int = 300) -> str:
    """Whitespace-collapsed text clipped to ``max_chars`` (the unit that is translated)."""
    t = _WS.sub(" ", text or "").strip()
    if len(t) > max_chars:
        t = t[:max_chars].rstrip()
    return t


def text_key(lang: str, text: str) -> str:
    return hashlib.sha1(f"{norm_lang(lang)}\n{text}".encode()).hexdigest()


class TranslationStore:
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

    # ------------------------------------------------------------------ cache

    def get(self, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM translations WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def get_many(self, keys: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        keys = list(dict.fromkeys(keys))
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            with self._lock:
                rows = self._db.execute(
                    f"SELECT * FROM translations WHERE key IN ({','.join('?' * len(chunk))})",
                    chunk).fetchall()
            out.update({r["key"]: dict(r) for r in rows})
        return out

    def enqueue(self, items: list[dict], now: datetime) -> int:
        """Add texts (dicts with key, lang, text, prio, sort_ts, english); English rows are
        stored as passthrough. Existing rows only get a better priority. Returns new rows."""
        ts = iso(now)
        added = 0
        with self._lock, self._db:
            for it in items:
                english = bool(it.get("english"))
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO translations (key, src_lang, text, text_en, model, "
                    "status, prio, sort_ts, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (it["key"], norm_lang(it["lang"]), it["text"],
                     it["text"] if english else None, None,
                     "passthrough" if english else "pending", int(it.get("prio", 9)),
                     it.get("sort_ts") or "", ts, ts))
                if cur.rowcount:
                    added += 1
                elif not english:
                    self._db.execute(
                        "UPDATE translations SET prio=MIN(prio, ?), "
                        "sort_ts=MAX(sort_ts, ?) WHERE key=? AND status IN ('pending','failed')",
                        (int(it.get("prio", 9)), it.get("sort_ts") or "", it["key"]))
        return added

    def due(self, limit: int, now: datetime, retry_after: timedelta,
            max_attempts: int) -> list[dict]:
        """Texts to translate: pending, then failed ones whose retry time has come;
        best priority first, most recent first within a priority."""
        cutoff = iso(now - retry_after)
        with self._lock:
            rows = self._db.execute(
                "SELECT key, src_lang, text, attempts FROM translations WHERE "
                "status='pending' OR (status='failed' AND attempts < ? AND updated_at <= ?) "
                "ORDER BY prio, sort_ts DESC, key LIMIT ?",
                (max_attempts, cutoff, limit)).fetchall()
        return [dict(r) for r in rows]

    def queue_size(self, now: datetime, retry_after: timedelta, max_attempts: int) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM translations WHERE status='pending' OR "
                "(status='failed' AND attempts < ? AND updated_at <= ?)",
                (max_attempts, iso(now - retry_after))).fetchone()
        return int(row[0])

    def save_done(self, results: dict[str, str], model: str, now: datetime) -> None:
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE translations SET text_en=?, model=?, status='done', error=NULL, "
                "attempts=attempts+1, updated_at=? WHERE key=?",
                [(en, model, iso(now), k) for k, en in results.items()])

    def save_failed(self, keys: list[str], error: str, now: datetime) -> None:
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE translations SET status='failed', error=?, attempts=attempts+1, "
                "updated_at=? WHERE key=? AND status != 'done'",
                [(error[:300], iso(now), k) for k in keys])

    def status_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT status, COUNT(*) FROM translations GROUP BY 1").fetchall()
        return {r[0]: r[1] for r in rows}

    def last_failures(self, limit: int = 5) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT key, src_lang, substr(text, 1, 80) AS text, attempts, error, updated_at "
                "FROM translations WHERE status='failed' ORDER BY updated_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ refs

    def set_refs(self, refs: list[tuple[str, str, str]], now: datetime) -> None:
        """(ref, key, lang) rows; a ref points at the text currently in that field."""
        ts = iso(now)
        with self._lock, self._db:
            self._db.executemany(
                "INSERT INTO refs (ref, key, lang, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(ref) DO UPDATE SET key=excluded.key, lang=excluded.lang, "
                "updated_at=excluded.updated_at WHERE refs.key != excluded.key "
                "OR refs.lang != excluded.lang", [(r, k, norm_lang(lg), ts) for r, k, lg in refs])

    def existing_refs(self, refs: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        refs = list(dict.fromkeys(refs))
        for i in range(0, len(refs), 500):
            chunk = refs[i:i + 500]
            with self._lock:
                rows = self._db.execute(
                    f"SELECT ref, key FROM refs WHERE ref IN ({','.join('?' * len(chunk))})",
                    chunk).fetchall()
            out.update({r[0]: r[1] for r in rows})
        return out

    def lookup_refs(self, refs: list[str]) -> dict[str, dict]:
        """{ref: {lang, text, text_en, status}} for known refs (text_en None if not ready)."""
        out: dict[str, dict] = {}
        refs = list(dict.fromkeys(refs))
        for i in range(0, len(refs), 500):
            chunk = refs[i:i + 500]
            with self._lock:
                rows = self._db.execute(
                    "SELECT r.ref, r.lang, t.text, t.text_en, t.status FROM refs r "
                    "LEFT JOIN translations t ON t.key = r.key "
                    f"WHERE r.ref IN ({','.join('?' * len(chunk))})", chunk).fetchall()
            out.update({r[0]: {"lang": r[1], "text": r[2], "text_en": r[3],
                               "status": r[4]} for r in rows})
        return out

    # ------------------------------------------------------------------ call log

    def log_call(self, ts: float, *, ok: bool, n_items: int, n_ok: int, cost_usd: float = 0.0,
                 latency_s: float | None = None, model: str | None = None,
                 error: str | None = None) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?)",
                             (ts, int(ok), n_items, n_ok, cost_usd, latency_s, model,
                              error[:300] if error else None))

    def call_times_since(self, since_ts: float) -> list[float]:
        with self._lock:
            rows = self._db.execute("SELECT ts FROM calls WHERE ts >= ? ORDER BY ts",
                                    (since_ts,)).fetchall()
        return [r[0] for r in rows]

    def call_stats(self, since_ts: float) -> dict:
        with self._lock:
            r = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(ok),0), COALESCE(SUM(cost_usd),0), "
                "AVG(latency_s), COALESCE(SUM(n_ok),0) FROM calls WHERE ts >= ?",
                (since_ts,)).fetchone()
        return {"calls": int(r[0]), "ok": int(r[1]), "cost_usd": round(float(r[2]), 6),
                "avg_latency_s": None if r[3] is None else round(float(r[3]), 2),
                "items_translated": int(r[4])}
