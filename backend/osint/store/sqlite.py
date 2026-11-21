"""SQLite store for relational state: sources, cursors, tracks, quarantine, events."""

import json
import sqlite3
import threading
from datetime import UTC, datetime
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

# Events (spec §3): one row per de-duplicated event; event_documents links contributing
# documents (role primary | mention | gdelt); event_fields is the per-field provenance;
# gdelt_decisions records every accept/reject of a GDELT row (benchmark substrate).
# Timestamps are UTC ISO-8601 strings with milliseconds (lexically sortable). H3 cells are
# stored as hex strings. cluster_state is the clusterer's private warm-start state.
EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, category TEXT NOT NULL, subcategory TEXT, cameo_root TEXT,
  title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', actors TEXT NOT NULL DEFAULT '[]',
  lat REAL, lon REAL, precision TEXT, place_name TEXT, country TEXT, geo_confidence REAL,
  h3_3 TEXT, h3_5 TEXT, h3_7 TEXT, t_start TEXT NOT NULL, t_end TEXT NOT NULL,
  status TEXT NOT NULL, confidence REAL NOT NULL, version INTEGER NOT NULL,
  mention_count INTEGER NOT NULL, source_diversity INTEGER NOT NULL,
  gdelt_ids TEXT NOT NULL DEFAULT '[]', first_seen TEXT NOT NULL, updated_at TEXT NOT NULL,
  cluster_state TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS events_updated ON events(updated_at);
CREATE INDEX IF NOT EXISTS events_category ON events(category);
CREATE INDEX IF NOT EXISTS events_h3_3 ON events(h3_3);
CREATE INDEX IF NOT EXISTS events_status ON events(status);
CREATE INDEX IF NOT EXISTS events_t_end ON events(t_end);
CREATE TABLE IF NOT EXISTS event_documents (
  event_id TEXT NOT NULL, doc_id TEXT NOT NULL, source TEXT NOT NULL, url TEXT NOT NULL,
  title TEXT NOT NULL, published_at TEXT NOT NULL, language TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL, added_at TEXT NOT NULL, PRIMARY KEY (event_id, doc_id));
CREATE INDEX IF NOT EXISTS event_documents_doc ON event_documents(doc_id);
CREATE INDEX IF NOT EXISTS event_documents_added ON event_documents(added_at);
CREATE TABLE IF NOT EXISTS event_fields (
  event_id TEXT NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL, doc_id TEXT NOT NULL,
  quote TEXT NOT NULL DEFAULT '', PRIMARY KEY (event_id, field, value, doc_id));
CREATE TABLE IF NOT EXISTS gdelt_decisions (
  gdelt_event_id TEXT PRIMARY KEY, decision TEXT NOT NULL, reason TEXT NOT NULL,
  event_id TEXT, doc_id TEXT, category TEXT, decided_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS gdelt_decisions_at ON gdelt_decisions(decided_at);
"""

EVENT_COLUMNS = (
    "id", "category", "subcategory", "cameo_root", "title", "summary", "actors", "lat", "lon",
    "precision", "place_name", "country", "geo_confidence", "h3_3", "h3_5", "h3_7", "t_start",
    "t_end", "status", "confidence", "version", "mention_count", "source_diversity",
    "gdelt_ids", "first_seen", "updated_at", "cluster_state",
)
_JSON_DEFAULTS: dict[str, object] = {"actors": [], "gdelt_ids": [], "cluster_state": {}}
_UPSERT_EVENT = (
    f"INSERT INTO events ({', '.join(EVENT_COLUMNS)}) "
    f"VALUES ({','.join('?' * len(EVENT_COLUMNS))}) ON CONFLICT(id) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}" for c in EVENT_COLUMNS if c not in ("id", "first_seen"))
)
_EVENT_DOC_COLUMNS = ("event_id", "doc_id", "source", "url", "title", "published_at",
                      "language", "role", "added_at")
SLIM_EVENT_COLUMNS = ("id", "lat", "lon", "precision", "category", "status", "confidence",
                      "title", "mention_count", "source_diversity", "t_start", "t_end",
                      "updated_at")


def iso(dt: datetime) -> str:
    """Canonical timestamp text for the events tables (UTC, milliseconds)."""
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def _json_col(value: object, column: str) -> str:
    if value is None:
        value = _JSON_DEFAULTS[column]
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _event_out(row: sqlite3.Row, *, keep_state: bool = False) -> dict:
    d = dict(row)
    for c in _JSON_DEFAULTS:
        if c in d:
            d[c] = json.loads(d[c])
    if not keep_state:
        d.pop("cluster_state", None)
    return d


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
        self._db.executescript(EVENTS_SCHEMA)

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

    # ------------------------------------------------------------------ events

    def save_events(self, events: list[dict], documents: list[dict] | None = None,
                    fields: list[dict] | None = None,
                    decisions: list[dict] | None = None) -> None:
        """Persist one clustering pass atomically. ``events`` are full event dicts (JSON
        columns as Python objects); first_seen keeps its first value. Documents, fields and
        decisions are insert-if-absent (re-running a pass is harmless)."""
        ev_rows = [
            tuple(_json_col(e.get(c), c) if c in _JSON_DEFAULTS else e.get(c)
                  for c in EVENT_COLUMNS)
            for e in events
        ]
        doc_rows = [tuple(d.get(c, "") for c in _EVENT_DOC_COLUMNS) for d in documents or ()]
        field_rows = [(f["event_id"], f["field"], str(f["value"]), f["doc_id"],
                       f.get("quote") or "") for f in fields or ()]
        dec_rows = [(d["gdelt_event_id"], d["decision"], d["reason"], d.get("event_id"),
                     d.get("doc_id"), d.get("category"), d["decided_at"])
                    for d in decisions or ()]
        with self._lock, self._db:
            if ev_rows:
                self._db.executemany(_UPSERT_EVENT, ev_rows)
            if doc_rows:
                self._db.executemany(
                    f"INSERT OR IGNORE INTO event_documents ({', '.join(_EVENT_DOC_COLUMNS)}) "
                    f"VALUES ({','.join('?' * len(_EVENT_DOC_COLUMNS))})", doc_rows)
            if field_rows:
                self._db.executemany(
                    "INSERT OR IGNORE INTO event_fields VALUES (?,?,?,?,?)", field_rows)
            if dec_rows:
                self._db.executemany(
                    "INSERT OR IGNORE INTO gdelt_decisions VALUES (?,?,?,?,?,?,?)", dec_rows)

    def upsert_events(self, events: list[dict]) -> None:
        self.save_events(events)

    def add_event_documents(self, documents: list[dict]) -> None:
        self.save_events([], documents=documents)

    def add_event_fields(self, fields: list[dict]) -> None:
        self.save_events([], fields=fields)

    def record_gdelt_decision(self, gdelt_event_id: str, decision: str, reason: str,
                              event_id: str | None = None, doc_id: str | None = None,
                              category: str | None = None,
                              decided_at: datetime | None = None) -> None:
        if decision not in ("accepted", "rejected"):
            raise ValueError("decision must be 'accepted' or 'rejected'")
        self.save_events([], decisions=[{
            "gdelt_event_id": gdelt_event_id, "decision": decision, "reason": reason,
            "event_id": event_id, "doc_id": doc_id, "category": category,
            "decided_at": iso(decided_at or utcnow())}])

    def get_event(self, event_id: str) -> dict | None:
        """Full event: columns plus fields (with quotes) and documents (with links)."""
        with self._lock:
            row = self._db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                return None
            docs = self._db.execute(
                "SELECT doc_id, source, url, title, published_at, language, role "
                "FROM event_documents WHERE event_id=? ORDER BY published_at, doc_id",
                (event_id,)).fetchall()
            fields = self._db.execute(
                "SELECT field, value, doc_id, quote FROM event_fields WHERE event_id=? "
                "ORDER BY field, value, doc_id", (event_id,)).fetchall()
        out = _event_out(row)
        out["documents"] = [dict(r) for r in docs]
        out["fields"] = [dict(r) for r in fields]
        return out

    def list_events(self, *, bbox: tuple[float, float, float, float] | None = None,
                    since: datetime | None = None, category: str | None = None,
                    status: str | None = None, min_confidence: float | None = None,
                    limit: int = 1000, slim: bool = True) -> list[dict]:
        """Events, most recently updated first. bbox = (west, south, east, north); west > east
        wraps the antimeridian. Comma-separated category/status values match any of them."""
        where: list[str] = []
        params: list[object] = []
        if bbox is not None:
            w, s, e, n = bbox
            where.append("lat IS NOT NULL AND lat BETWEEN ? AND ?")
            params += [s, n]
            if w <= e:
                where.append("lon BETWEEN ? AND ?")
            else:
                where.append("(lon >= ? OR lon <= ?)")
            params += [w, e]
        if since is not None:
            where.append("updated_at >= ?")
            params.append(iso(since))
        for col, raw in (("category", category), ("status", status)):
            values = [v.strip() for v in (raw or "").split(",") if v.strip()]
            if values:
                where.append(f"{col} IN ({','.join('?' * len(values))})")
                params += values
        if min_confidence is not None:
            where.append("confidence >= ?")
            params.append(min_confidence)
        cols = ", ".join(SLIM_EVENT_COLUMNS) if slim else "*"
        sql = f"SELECT {cols} FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(max(0, int(limit)))
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [_event_out(r) for r in rows]

    def recent_events(self, since: datetime) -> list[dict]:
        """Events with t_end >= since, including cluster_state (clusterer warm start)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events WHERE t_end >= ? ORDER BY t_end", (iso(since),)
            ).fetchall()
        return [_event_out(r, keep_state=True) for r in rows]

    def event_doc_ids_since(self, since: datetime) -> set[str]:
        """Document ids attached to an event or adjudicated (GDELT) since ``since``."""
        s = iso(since)
        with self._lock:
            rows = self._db.execute(
                "SELECT doc_id FROM event_documents WHERE added_at >= ? UNION "
                "SELECT doc_id FROM gdelt_decisions WHERE decided_at >= ? AND doc_id IS NOT NULL",
                (s, s)).fetchall()
        return {r[0] for r in rows}

    def event_stats(self) -> dict:
        """Counts by status/category and GDELT decisions by reason (scorecard v1)."""
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            by_status = self._db.execute(
                "SELECT status, COUNT(*) FROM events GROUP BY status").fetchall()
            by_cat = self._db.execute(
                "SELECT category, COUNT(*) FROM events GROUP BY category").fetchall()
            docs = self._db.execute(
                "SELECT role, COUNT(*) FROM event_documents GROUP BY role").fetchall()
            dec = self._db.execute(
                "SELECT decision, reason, COUNT(*) FROM gdelt_decisions "
                "GROUP BY decision, reason").fetchall()
        gdelt: dict[str, dict] = {"accepted": {"total": 0, "by_reason": {}},
                                  "rejected": {"total": 0, "by_reason": {}}}
        for decision, reason, n in dec:
            g = gdelt.setdefault(decision, {"total": 0, "by_reason": {}})
            g["total"] += n
            g["by_reason"][reason] = n
        adjudicated = gdelt["accepted"]["total"] + gdelt["rejected"]["total"]
        return {
            "events": total,
            "by_status": {r[0]: r[1] for r in by_status},
            "by_category": {r[0]: r[1] for r in by_cat},
            "documents_by_role": {r[0]: r[1] for r in docs},
            "gdelt": {**gdelt, "adjudicated": adjudicated,
                      "acceptance_rate": (round(gdelt["accepted"]["total"] / adjudicated, 4)
                                          if adjudicated else None)},
        }

    def close(self) -> None:
        with self._lock:
            self._db.close()
