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

# Sensor corroboration (spec §3 Corroboration, §5 stage 6). corroborations: one row per
# piece of sensor evidence linked to an event (kind fire|quake|aircraft|ship|ais_gap|imagery;
# ref_id = observation id, track id or "<track>@<gap start>"). event_corroboration: the
# per-event roll-up (score, kinds) that the API and the confidence formula read.
# sensor_events: events created by a sensor detector (source 'sensor'); dedupe_key is
# "<detector>:<cell>:<window>" and makes detection re-runs idempotent.
CORROBORATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS corroborations (
  event_id TEXT NOT NULL, kind TEXT NOT NULL, ref_id TEXT NOT NULL, rule TEXT NOT NULL,
  distance_km REAL, dt_minutes REAL, score REAL NOT NULL, details TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (event_id, kind, ref_id));
CREATE INDEX IF NOT EXISTS corroborations_kind ON corroborations(kind);
CREATE TABLE IF NOT EXISTS event_corroboration (
  event_id TEXT PRIMARY KEY, score REAL NOT NULL, kinds TEXT NOT NULL DEFAULT '[]',
  evidence INTEGER NOT NULL, corroborated INTEGER NOT NULL, evaluated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sensor_events (
  event_id TEXT PRIMARY KEY, detector TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE,
  cell TEXT NOT NULL, t_start TEXT NOT NULL, t_end TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sensor_events_cell ON sensor_events(detector, cell, t_end);
"""
# Extraction verdicts merged into events (pipeline.events applies them; source of truth is
# data_dir/extraction.sqlite). fingerprint makes re-application idempotent.
VERIFICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_verification (
  event_id TEXT PRIMARY KEY, verdict TEXT NOT NULL, reasons TEXT NOT NULL DEFAULT '[]',
  quotes TEXT NOT NULL DEFAULT '[]', fingerprint TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS event_verification_verdict ON event_verification(verdict);
"""
# Impact sites and affected areas (osint.pipeline.impact_job, worker only). One row per
# event: sites (resolved impact sites with evidence quotes), best (the site that located the
# event, or null), areas (GeoJSON geometries, each with its basis), origin (the event's
# location before the impact upgrade). The bbox columns cover sites + areas (layer queries);
# input_fp skips recomputation when nothing that feeds the cascade changed.
IMPACT_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_impacts (
  event_id TEXT PRIMARY KEY, version INTEGER NOT NULL, sites TEXT NOT NULL DEFAULT '[]',
  best TEXT, areas TEXT NOT NULL DEFAULT '[]', origin TEXT, input_fp TEXT NOT NULL,
  min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL,
  computed_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS event_impacts_updated ON event_impacts(updated_at);
"""
# Status history (event timeline): every insert and every status change of a news or sensor
# event, written by triggers so no writer (clusterer, corroborator, sensor detector) has to
# know about it. ``source`` is 'insert', 'change' or 'baseline' (the status an event already
# had when history recording started: its true start time is unknown). Historical (curated
# dataset) rows are not tracked. Created after the origin migration (the triggers read it).
STATUS_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_status_history (
  event_id TEXT NOT NULL, status TEXT NOT NULL, prev_status TEXT, at TEXT NOT NULL,
  source TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS event_status_history_event ON event_status_history(event_id, at);
CREATE TRIGGER IF NOT EXISTS events_status_ins AFTER INSERT ON events
  WHEN NEW.origin != 'historical' BEGIN
  INSERT INTO event_status_history VALUES (NEW.id, NEW.status, NULL,
    strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), 'insert');
END;
CREATE TRIGGER IF NOT EXISTS events_status_upd AFTER UPDATE OF status ON events
  WHEN NEW.origin != 'historical' AND NEW.status IS NOT OLD.status BEGIN
  INSERT INTO event_status_history VALUES (NEW.id, NEW.status, OLD.status,
    strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), 'change');
END;
CREATE TRIGGER IF NOT EXISTS events_status_del AFTER DELETE ON events BEGIN
  DELETE FROM event_status_history WHERE event_id = OLD.id;
END;
"""
CORROBORATION_KINDS = ("fire", "quake", "aircraft", "ship", "ais_gap", "imagery")

EVENT_COLUMNS = (
    "id", "category", "subcategory", "cameo_root", "title", "summary", "actors", "lat", "lon",
    "precision", "place_name", "country", "geo_confidence", "h3_3", "h3_5", "h3_7", "t_start",
    "t_end", "status", "confidence", "version", "mention_count", "source_diversity",
    "gdelt_ids", "first_seen", "updated_at", "cluster_state", "origin",
)
# Who owns an event row. Only ``news`` events (built by the clusterer from articles) are
# ever cleared, migrated or rebuilt; ``sensor`` (pipeline.sensor_events) and ``historical``
# (curated datasets such as UCDP GED) rows are never touched by event maintenance.
ORIGINS = ("news", "sensor", "historical")
HISTORICAL_STATUS = "historical"
_JSON_DEFAULTS: dict[str, object] = {"actors": [], "gdelt_ids": [], "cluster_state": {}}
_UPSERT_EVENT = (
    f"INSERT INTO events ({', '.join(EVENT_COLUMNS)}) "
    f"VALUES ({','.join('?' * len(EVENT_COLUMNS))}) ON CONFLICT(id) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}" for c in EVENT_COLUMNS
                if c not in ("id", "first_seen", "origin"))
    # a writer that does not know the origin (default news) never demotes a sensor or
    # historical row
    + ", origin=CASE WHEN events.origin = 'news' THEN excluded.origin ELSE events.origin END"
)
_EVENT_DOC_COLUMNS = ("event_id", "doc_id", "source", "url", "title", "published_at",
                      "language", "role", "added_at")
SLIM_EVENT_COLUMNS = ("id", "lat", "lon", "precision", "category", "status", "confidence",
                      "title", "mention_count", "source_diversity", "t_start", "t_end",
                      "updated_at", "place_name", "country", "subcategory")


def iso(dt: datetime) -> str:
    """Canonical timestamp text for the events tables (UTC, milliseconds)."""
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def _json_col(value: object, column: str) -> str:
    if value is None:
        value = _JSON_DEFAULTS[column]
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _dump_json(value: object) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False,
                                                  separators=(",", ":"))


def _bbox_cols(bbox) -> tuple:
    """(min_lat, min_lon, max_lat, max_lon) from a (west, south, east, north) bbox."""
    if not bbox:
        return (None, None, None, None)
    w, s, e, n = bbox
    return (s, w, n, e)


def _impact_out(row: sqlite3.Row) -> dict:
    return {"event_id": row["event_id"], "version": row["version"],
            "sites": json.loads(row["sites"] or "[]"),
            "best": json.loads(row["best"]) if row["best"] else None,
            "areas": json.loads(row["areas"] or "[]"),
            "origin": json.loads(row["origin"]) if row["origin"] else None,
            "input_fp": row["input_fp"], "computed_at": row["computed_at"],
            "updated_at": row["updated_at"]}


def event_origin(e: dict) -> str:
    """Explicit ``origin`` or the one implied by the row (curated dataset -> historical)."""
    origin = e.get("origin")
    if origin in ORIGINS:
        return origin
    state = e.get("cluster_state") or {}
    if e.get("status") == HISTORICAL_STATUS or (
            isinstance(state, dict) and state.get("source") == "ucdp"):
        return "historical"
    if isinstance(state, dict) and state.get("sensor"):
        return "sensor"
    return "news"


def _event_tuple(e: dict) -> tuple:
    return tuple(_json_col(e.get(c), c) if c in _JSON_DEFAULTS
                 else (e.get(c) or "") if c == "summary"
                 else event_origin(e) if c == "origin" else e.get(c)
                 for c in EVENT_COLUMNS)


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


# WAL file is truncated back to this size after a checkpoint (it reached 770 MB unbounded)
JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024
BUSY_TIMEOUT_S = 30.0
WRITE_CHUNK = 5000  # bulk writers commit in chunks of this many rows


def connect(path: Path | str, *, read_only: bool = False,
            timeout: float = BUSY_TIMEOUT_S) -> sqlite3.Connection:
    """A connection with the platform's settings (busy timeout, WAL size cap)."""
    if read_only:
        return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True,
                               timeout=timeout, check_same_thread=False)
    db = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
    db.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}")
    return db


class SqliteStore:
    def __init__(self, path: Path, *, migrate: bool = True) -> None:
        """``migrate=False`` (the API process) skips the data-healing UPDATEs, which the
        worker owns; schema creation and the one-time column add still run (idempotent)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._db.executescript(EVENTS_SCHEMA)
        self._db.executescript(CORROBORATION_SCHEMA)
        self._db.executescript(VERIFICATION_SCHEMA)
        self._db.executescript(IMPACT_SCHEMA)
        self._migrate_origin(heal=migrate)
        self._db.executescript(STATUS_HISTORY_SCHEMA)
        if migrate:
            self._status_baseline()

    def _migrate_origin(self, heal: bool = True) -> None:
        """Add/backfill ``events.origin`` (idempotent; cheap on later starts)."""
        with self._lock:
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(events)")}
        if "origin" in cols and not heal:
            return
        with self._lock, self._db:
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(events)")}
            if "origin" not in cols:
                self._db.execute(
                    "ALTER TABLE events ADD COLUMN origin TEXT NOT NULL DEFAULT 'news'")
                self._db.execute(
                    "UPDATE events SET origin='historical' WHERE status=? OR "
                    "json_extract(cluster_state, '$.source') = 'ucdp'", (HISTORICAL_STATUS,))
            else:  # heal rows written by code that did not know the column
                self._db.execute(
                    "UPDATE events SET origin='historical' WHERE origin='news' AND "
                    "(status=? OR id LIKE 'ucdp-%')", (HISTORICAL_STATUS,))
            self._db.execute(
                "UPDATE events SET origin='sensor' WHERE origin='news' AND id IN "
                "(SELECT event_id FROM sensor_events)")
            self._db.execute("CREATE INDEX IF NOT EXISTS events_origin ON events(origin)")

    def _status_baseline(self) -> None:
        """One 'baseline' history row (status as of now) for events that predate history
        recording; idempotent (events with any history row are skipped)."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO event_status_history SELECT id, status, NULL, ?, 'baseline' "
                "FROM events WHERE origin != 'historical' AND id NOT IN "
                "(SELECT event_id FROM event_status_history)", (iso(utcnow()),))

    def status_history(self, event_id: str) -> list[dict]:
        """Recorded status changes of an event, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT status, prev_status, at, source FROM event_status_history "
                "WHERE event_id=? ORDER BY at, rowid", (event_id,)).fetchall()
        return [dict(r) for r in rows]

    def checkpoint(self) -> dict[str, int]:
        """``wal_checkpoint(TRUNCATE)``: fold the WAL into the database and truncate it
        (Runtime maintenance runs it hourly). busy=1 when a reader blocked completion."""
        with self._lock:
            busy, log_pages, done = self._db.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return {"busy": busy, "log": log_pages, "checkpointed": done}

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
        ev_rows = [_event_tuple(e) for e in events]
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
                    limit: int = 1000, slim: bool = True, include_unlocated: bool = True,
                    map_default: bool = False, origin: str | None = None) -> list[dict]:
        """Events, most recently updated first. bbox = (west, south, east, north); west > east
        wraps the antimeridian. Comma-separated category/status values match any of them.

        ``include_unlocated=False`` drops events without coordinates. ``map_default`` keeps
        only events worth showing on the map: status multi-source / sensor-corroborated /
        official, or reported with city/point precision and confidence >= 0.5."""
        where: list[str] = []
        params: list[object] = []
        if not include_unlocated:
            where.append("lat IS NOT NULL")
        if map_default:
            where.append("(status IN ('multi-source', 'sensor-corroborated', 'official') OR "
                         "(status = 'reported' AND precision IN ('city', 'point') "
                         "AND confidence >= 0.5))")
            where.append("id NOT IN (SELECT event_id FROM event_verification "
                         "WHERE verdict = 'unsupported')")
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
        for col, raw in (("category", category), ("status", status), ("origin", origin)):
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

    def search_events(self, q: str, *, since: datetime | None = None, limit: int = 200
                      ) -> list[dict]:
        """Slim non-historical events whose title, place or actors contain ``q``
        (case-insensitive), most recent first (the intel pane's actor / place pivot)."""
        like = "%" + q.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        where = ["origin != 'historical'",
                 ("(title LIKE ? ESCAPE '!' OR place_name LIKE ? ESCAPE '!' "
                 "OR actors LIKE ? ESCAPE '!')")]
        params: list[object] = [like, like, like]
        if since is not None:
            where.append("t_end >= ?")
            params.append(iso(since))
        params.append(max(0, int(limit)))
        with self._lock:
            rows = self._db.execute(
                f"SELECT {', '.join(SLIM_EVENT_COLUMNS)}, actors FROM events WHERE "
                f"{' AND '.join(where)} ORDER BY t_end DESC LIMIT ?", params).fetchall()
        return [_event_out(r) for r in rows]

    def field_values(self, event_ids: list[str], fields: tuple[str, ...]) -> dict[str, dict]:
        """event id -> {field: first value} for the given extracted fields."""
        out: dict[str, dict] = {}
        fmarks = ",".join("?" * len(fields))
        for i in range(0, len(event_ids), 800):
            chunk = event_ids[i: i + 800]
            with self._lock:
                rows = self._db.execute(
                    f"SELECT event_id, field, value FROM event_fields WHERE field IN ({fmarks}) "
                    f"AND event_id IN ({','.join('?' * len(chunk))})",
                    (*fields, *chunk)).fetchall()
            for eid, f, v in rows:
                out.setdefault(eid, {}).setdefault(f, v)
        return out

    def event_actors(self, event_ids: list[str]) -> dict[str, list]:
        """event id -> actors list (the slim list rows do not carry it)."""
        out: dict[str, list] = {}
        for i in range(0, len(event_ids), 800):
            chunk = event_ids[i: i + 800]
            with self._lock:
                rows = self._db.execute(
                    f"SELECT id, actors FROM events WHERE id IN ({','.join('?' * len(chunk))})",
                    chunk).fetchall()
            for eid, raw in rows:
                try:
                    out[eid] = json.loads(raw or "[]")
                except ValueError:
                    out[eid] = []
        return out

    def recent_events(self, since: datetime, origins: tuple[str, ...] = ("news",)
                      ) -> list[dict]:
        """Events of ``origins`` (default: news only) with t_end >= since, including
        cluster_state (clusterer warm start)."""
        marks = ",".join("?" * len(origins))
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM events WHERE t_end >= ? AND origin IN ({marks}) "
                f"ORDER BY t_end", (iso(since), *origins)).fetchall()
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

    # -------------------------------------------------------- event maintenance

    def gdelt_only_event_ids(self) -> list[str]:
        """News events with no article document (only GDELT rows): never valid."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM events e WHERE origin = 'news' AND NOT EXISTS "
                "(SELECT 1 FROM event_documents d WHERE d.event_id = e.id "
                "AND d.role != 'gdelt') AND id NOT IN (SELECT event_id FROM sensor_events)"
            ).fetchall()
        return [r[0] for r in rows]

    def legacy_event_ids(self, version: int) -> list[str]:
        """News events written by an older clusterer (cluster_state.v < ``version``)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM events WHERE origin = 'news' AND "
                "COALESCE(json_extract(cluster_state, '$.v'), 0) < ? "
                "AND id NOT IN (SELECT event_id FROM sensor_events)", (version,)).fetchall()
        return [r[0] for r in rows]

    def news_events_before_generation(self, generation: int) -> list[str]:
        """News events not written by rebuild ``generation`` or later (``$.gen``)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM events WHERE origin = 'news' AND "
                "COALESCE(json_extract(cluster_state, '$.gen'), 0) < ? "
                "AND id NOT IN (SELECT event_id FROM sensor_events)", (generation,)
            ).fetchall()
        return [r[0] for r in rows]

    def news_primary_event_ids(self) -> dict[str, str]:
        """primary document id -> news event id (event identity across rebuilds)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT d.doc_id, d.event_id FROM event_documents d JOIN events e "
                "ON e.id = d.event_id WHERE d.role = 'primary' AND e.origin = 'news'"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def reset_event_links(self, event_ids: list[str]) -> None:
        """Drop the document links and field provenance of news events about to be
        re-derived (a rebuild re-creating an event under its old id)."""
        with self._lock, self._db:
            for i in range(0, len(event_ids), 900):
                chunk = event_ids[i: i + 900]
                marks = ",".join("?" * len(chunk))
                ids = [r[0] for r in self._db.execute(
                    f"SELECT id FROM events WHERE origin = 'news' AND id IN ({marks})",
                    chunk)]
                if not ids:
                    continue
                m = ",".join("?" * len(ids))
                self._db.execute(f"DELETE FROM event_documents WHERE event_id IN ({m})", ids)
                self._db.execute(f"DELETE FROM event_fields WHERE event_id IN ({m})", ids)

    def clear_gdelt_decisions(self) -> int:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM gdelt_decisions").rowcount

    def doc_event_places(self) -> dict[str, tuple]:
        """doc_id -> (event title, place_name) for every news event document."""
        with self._lock:
            rows = self._db.execute(
                "SELECT d.doc_id, e.title, e.place_name FROM events e JOIN event_documents d "
                "ON d.event_id = e.id WHERE e.origin = 'news'").fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    def delete_events(self, event_ids: list[str], *, origin: str | None = "news") -> int:
        """Delete events with their documents, fields and corroboration rows. Only events
        of ``origin`` (default news; None = any) are deleted, other ids are ignored."""
        n = 0
        with self._lock, self._db:
            for i in range(0, len(event_ids), 900):
                chunk = event_ids[i: i + 900]
                marks = ",".join("?" * len(chunk))
                if origin is not None:
                    chunk = [r[0] for r in self._db.execute(
                        f"SELECT id FROM events WHERE origin = ? AND id IN ({marks})",
                        (origin, *chunk))]
                    if not chunk:
                        continue
                    marks = ",".join("?" * len(chunk))
                for table, col in (("event_documents", "event_id"),
                                   ("event_fields", "event_id"),
                                   ("corroborations", "event_id"),
                                   ("event_corroboration", "event_id"),
                                   ("event_verification", "event_id"),
                                   ("sensor_events", "event_id")):
                    self._db.execute(f"DELETE FROM {table} WHERE {col} IN ({marks})", chunk)
                n += self._db.execute(f"DELETE FROM events WHERE id IN ({marks})",
                                      chunk).rowcount
        return n

    def reject_orphan_gdelt_decisions(self, reason: str) -> int:
        """Accepted GDELT decisions whose event no longer exists -> rejected/``reason``."""
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE gdelt_decisions SET decision='rejected', reason=?, event_id=NULL "
                "WHERE decision='accepted' AND (event_id IS NULL OR event_id NOT IN "
                "(SELECT id FROM events))", (reason,)).rowcount

    def clear_news_events(self) -> int:
        """Remove every news event (origin news: never sensor or historical rows) with its
        documents, fields, corroborations, and all GDELT decisions."""
        news = ("(SELECT id FROM events WHERE origin = 'news' AND id NOT IN "
                "(SELECT event_id FROM sensor_events))")
        with self._lock, self._db:
            for table in ("event_documents", "event_fields", "corroborations",
                          "event_corroboration", "event_verification"):
                self._db.execute(f"DELETE FROM {table} WHERE event_id IN {news}")
            self._db.execute("DELETE FROM gdelt_decisions")
            return self._db.execute(f"DELETE FROM events WHERE id IN {news}").rowcount

    # ------------------------------------------------------------- verification

    def save_verifications(self, rows: list[dict]) -> None:
        """Upsert {event_id, verdict, reasons, quotes, fingerprint} rows."""
        now = iso(utcnow())
        with self._lock, self._db:
            self._db.executemany(
                "INSERT INTO event_verification VALUES (?,?,?,?,?,?) ON CONFLICT(event_id) "
                "DO UPDATE SET verdict=excluded.verdict, reasons=excluded.reasons, "
                "quotes=excluded.quotes, fingerprint=excluded.fingerprint, "
                "updated_at=excluded.updated_at",
                [(r["event_id"], r["verdict"],
                  json.dumps(r.get("reasons") or [], ensure_ascii=False),
                  json.dumps(r.get("quotes") or [], ensure_ascii=False),
                  r["fingerprint"], now) for r in rows])

    def type_mismatches_to_unsupported(self, cap: float) -> int:
        """Stored ``mismatch`` verdicts with an event-*type* reason become ``unsupported``
        (hidden from map_default) and their news event's confidence is capped at ``cap``."""
        with self._lock, self._db:
            ids = [r[0] for r in self._db.execute(
                "SELECT event_id FROM event_verification WHERE verdict = 'mismatch' AND "
                "EXISTS (SELECT 1 FROM json_each(reasons) WHERE value LIKE 'type:%')")]
            for i in range(0, len(ids), 900):
                chunk = ids[i: i + 900]
                marks = ",".join("?" * len(chunk))
                self._db.execute(f"UPDATE event_verification SET verdict = 'unsupported' "
                                 f"WHERE event_id IN ({marks})", chunk)
                self._db.execute(f"UPDATE events SET confidence = MIN(confidence, ?) "
                                 f"WHERE origin = 'news' AND id IN ({marks})", (cap, *chunk))
        return len(ids)

    def verifications(self, event_ids: list[str] | None = None) -> dict[str, dict]:
        """event id -> {verdict, reasons, quotes, fingerprint} (all rows when ids is None)."""
        if event_ids is None:
            with self._lock:
                rows = self._db.execute("SELECT * FROM event_verification").fetchall()
        else:
            rows = []
            for i in range(0, len(event_ids), 900):
                chunk = event_ids[i: i + 900]
                with self._lock:
                    rows += self._db.execute(
                        f"SELECT * FROM event_verification WHERE event_id IN "
                        f"({','.join('?' * len(chunk))})", chunk).fetchall()
        out = {}
        for r in rows:
            d = dict(r)
            d["reasons"], d["quotes"] = json.loads(d["reasons"]), json.loads(d["quotes"])
            out[d.pop("event_id")] = d
        return out

    # ------------------------------------------------------------ impact sites

    def save_impacts(self, rows: list[dict]) -> None:
        """Upsert event_impacts rows {event_id, version, sites, best, areas, origin,
        input_fp, bbox (west, south, east, north) or None, computed_at}."""
        now = iso(utcnow())
        with self._lock, self._db:
            self._db.executemany(
                "INSERT INTO event_impacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(event_id) DO UPDATE SET version=excluded.version, "
                "sites=excluded.sites, best=excluded.best, areas=excluded.areas, "
                "origin=excluded.origin, "
                "input_fp=excluded.input_fp, min_lat=excluded.min_lat, "
                "min_lon=excluded.min_lon, max_lat=excluded.max_lat, max_lon=excluded.max_lon,"
                " computed_at=excluded.computed_at, updated_at=excluded.updated_at",
                [(r["event_id"], int(r["version"]), _dump_json(r.get("sites") or []),
                  _dump_json(r.get("best")), _dump_json(r.get("areas") or []),
                  _dump_json(r.get("origin")), r["input_fp"], *_bbox_cols(r.get("bbox")),
                  r["computed_at"], now) for r in rows])

    def impacts(self, event_ids: list[str]) -> dict[str, dict]:
        """event id -> full impact record (sites, best, areas, origin, ...)."""
        rows = []
        for i in range(0, len(event_ids), 900):
            chunk = event_ids[i: i + 900]
            with self._lock:
                rows += self._db.execute(
                    f"SELECT * FROM event_impacts WHERE event_id IN "
                    f"({','.join('?' * len(chunk))})", chunk).fetchall()
        return {r["event_id"]: _impact_out(r) for r in rows}

    def impact_summaries(self, event_ids: list[str]) -> dict[str, dict]:
        """event id -> slim map fields: impact_precision / impact_lat / impact_lon (best
        site), impact_sites (number of sites) and has_area."""
        out: dict[str, dict] = {}
        for i in range(0, len(event_ids), 900):
            chunk = event_ids[i: i + 900]
            with self._lock:
                rows = self._db.execute(
                    "SELECT event_id, json_extract(best, '$.precision'), "
                    "json_extract(best, '$.lat'), json_extract(best, '$.lon'), "
                    "json_array_length(sites), json_array_length(areas) FROM event_impacts "
                    f"WHERE event_id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
            for r in rows:
                out[r[0]] = {"impact_precision": r[1], "impact_lat": r[2], "impact_lon": r[3],
                             "impact_sites": int(r[4] or 0), "has_area": bool(r[5])}
        return out

    def impacts_since(self, since: datetime) -> dict[str, dict]:
        """Impact records updated since ``since`` (the event builder applies them)."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM event_impacts WHERE updated_at >= ?",
                                    (iso(since),)).fetchall()
        return {r["event_id"]: _impact_out(r) for r in rows}

    def impact_fingerprints(self, event_ids: list[str]) -> dict[str, tuple]:
        """event id -> (version, input_fp, computed_at) of its stored impact record."""
        out: dict[str, tuple] = {}
        for i in range(0, len(event_ids), 900):
            chunk = event_ids[i: i + 900]
            with self._lock:
                rows = self._db.execute(
                    "SELECT event_id, version, input_fp, computed_at FROM event_impacts "
                    f"WHERE event_id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
            out.update({r[0]: (r[1], r[2], r[3]) for r in rows})
        return out

    def impacts_in_bbox(self, bbox: tuple[float, float, float, float] | None = None, *,
                        since: datetime | None = None, limit: int = 2000) -> list[dict]:
        """Impact records with sites or areas (plus their event's category / status /
        confidence / t_start) whose bounds intersect ``bbox`` (west, south, east, north;
        west > east wraps the antimeridian), most recently updated event first. Records
        of deleted events are skipped (inner join)."""
        where: list[str] = ["(i.sites != '[]' OR i.areas != '[]')"]
        params: list[object] = []
        if bbox is not None:
            w, s, e, n = bbox
            where.append("i.max_lat >= ? AND i.min_lat <= ?")
            params += [s, n]
            if w <= e:
                where.append("i.max_lon >= ? AND i.min_lon <= ?")
            else:
                where.append("(i.max_lon >= ? OR i.min_lon <= ?)")
            params += [w, e]
        if since is not None:
            where.append("e.updated_at >= ?")
            params.append(iso(since))
        params.append(max(0, int(limit)))
        with self._lock:
            rows = self._db.execute(
                "SELECT i.*, e.category AS e_category, e.status AS e_status, "
                "e.confidence AS e_confidence, e.t_start AS e_t_start, "
                "e.updated_at AS e_updated_at FROM event_impacts i "
                "JOIN events e ON e.id = i.event_id WHERE " + " AND ".join(where)
                + " ORDER BY e.updated_at DESC LIMIT ?", params).fetchall()
        out = []
        for r in rows:
            d = _impact_out(r)
            d.update({"category": r["e_category"], "status": r["e_status"],
                      "confidence": r["e_confidence"], "t_start": r["e_t_start"],
                      "event_updated_at": r["e_updated_at"]})
            out.append(d)
        return out

    # ------------------------------------------------------------ corroboration

    def save_corroborations(self, rows: list[dict], *,
                            replace_for: dict[str, set[str]] | None = None,
                            summaries: list[dict] | None = None,
                            events: list[dict] | None = None) -> dict[str, int]:
        """Upsert corroboration rows (unique per event/kind/ref; created_at is kept), the
        per-event roll-ups and ``events`` rows in one transaction.

        ``replace_for`` maps event id -> kinds the caller re-evaluated: stored rows of those
        kinds for that event that are not in ``rows`` are deleted (evidence that no longer
        matches). Returns the number of newly inserted rows per kind."""
        now = iso(utcnow())
        new: dict[str, int] = {}
        keep = {(r["event_id"], r["kind"], str(r["ref_id"])) for r in rows}
        with self._lock, self._db:
            if events:
                self._db.executemany(_UPSERT_EVENT, [_event_tuple(e) for e in events])
            for eid, kinds in (replace_for or {}).items():
                if not kinds:
                    continue
                marks = ",".join("?" * len(kinds))
                old = self._db.execute(
                    f"SELECT kind, ref_id FROM corroborations WHERE event_id=? "
                    f"AND kind IN ({marks})", (eid, *sorted(kinds))).fetchall()
                gone = [(eid, k, ref) for k, ref in old if (eid, k, ref) not in keep]
                if gone:
                    self._db.executemany(
                        "DELETE FROM corroborations WHERE event_id=? AND kind=? AND ref_id=?",
                        gone)
            existing: set[tuple[str, str, str]] = set()
            ids = sorted({k[0] for k in keep})
            for i in range(0, len(ids), 900):
                chunk = ids[i: i + 900]
                existing.update(tuple(r) for r in self._db.execute(
                    f"SELECT event_id, kind, ref_id FROM corroborations WHERE event_id IN "
                    f"({','.join('?' * len(chunk))})", chunk).fetchall())
            for key in keep - existing:
                new[key[1]] = new.get(key[1], 0) + 1
            self._db.executemany(
                "INSERT INTO corroborations VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(event_id, kind, ref_id) DO UPDATE SET rule=excluded.rule, "
                "distance_km=excluded.distance_km, dt_minutes=excluded.dt_minutes, "
                "score=excluded.score, details=excluded.details, updated_at=excluded.updated_at",
                [(r["event_id"], r["kind"], str(r["ref_id"]), r["rule"], r.get("distance_km"),
                  r.get("dt_minutes"), float(r["score"]),
                  json.dumps(r.get("details") or {}, ensure_ascii=False,
                             separators=(",", ":"), default=str), now, now) for r in rows])
            if summaries:
                self._db.executemany(
                    "INSERT INTO event_corroboration VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(event_id) DO UPDATE SET score=excluded.score, "
                    "kinds=excluded.kinds, evidence=excluded.evidence, "
                    "corroborated=excluded.corroborated, evaluated_at=excluded.evaluated_at",
                    [(s["event_id"], float(s["score"]), json.dumps(sorted(s["kinds"])),
                      int(s["evidence"]), int(bool(s["corroborated"])), now)
                     for s in summaries])
        return new

    def list_corroborations(self, event_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, ref_id, rule, distance_km, dt_minutes, score, details, "
                "created_at FROM corroborations WHERE event_id=? "
                "ORDER BY score DESC, kind, ref_id", (event_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["details"] = json.loads(d["details"])
            out.append(d)
        return out

    def corroboration_rows(self, event_ids: list[str]) -> dict[str, list[dict]]:
        """event id -> [{kind, ref_id, score}] (input of the score roll-up)."""
        out: dict[str, list[dict]] = {}
        for i in range(0, len(event_ids), 900):
            chunk = event_ids[i: i + 900]
            with self._lock:
                rows = self._db.execute(
                    f"SELECT event_id, kind, ref_id, score FROM corroborations WHERE event_id "
                    f"IN ({','.join('?' * len(chunk))})", chunk).fetchall()
            for r in rows:
                out.setdefault(r[0], []).append({"kind": r[1], "ref_id": r[2], "score": r[3]})
        return out

    def corroboration_summaries(self, event_ids: list[str]) -> dict[str, dict]:
        """event id -> {score, kinds, evidence, corroborated[, detector]} for known ids."""
        out: dict[str, dict] = {}
        for i in range(0, len(event_ids), 900):
            chunk = event_ids[i: i + 900]
            marks = ",".join("?" * len(chunk))
            with self._lock:
                rows = self._db.execute(
                    f"SELECT event_id, score, kinds, evidence, corroborated FROM "
                    f"event_corroboration WHERE event_id IN ({marks})", chunk).fetchall()
                sensor = self._db.execute(
                    f"SELECT event_id, detector FROM sensor_events WHERE event_id IN ({marks})",
                    chunk).fetchall()
            for r in rows:
                out[r[0]] = {"score": r[1], "kinds": json.loads(r[2]), "evidence": r[3],
                             "corroborated": bool(r[4])}
            for eid, det in sensor:
                out.setdefault(eid, {"score": 0.0, "kinds": [], "evidence": 0,
                                     "corroborated": True})["detector"] = det
        return out

    def sensor_scores_since(self, since: datetime) -> dict[str, float]:
        """Roll-up score of every event evaluated since ``since`` (scorer warm start)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT event_id, score FROM event_corroboration WHERE evaluated_at >= ?",
                (iso(since),)).fetchall()
        return {r[0]: r[1] for r in rows}

    def sensor_event_ids(self) -> set[str]:
        with self._lock:
            rows = self._db.execute("SELECT event_id FROM sensor_events").fetchall()
        return {r[0] for r in rows}

    @staticmethod
    def _sensor_out(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        d["details"] = json.loads(d["details"])
        return d

    def find_sensor_event(self, detector: str, cells: list[str],
                          not_before: datetime) -> dict | None:
        """Latest sensor event of ``detector`` in one of ``cells`` whose t_end is at or
        after ``not_before`` (a continuing pattern extends it instead of a new event)."""
        if not cells:
            return None
        marks = ",".join("?" * len(cells))
        with self._lock:
            row = self._db.execute(
                f"SELECT * FROM sensor_events WHERE detector=? AND cell IN ({marks}) "
                f"AND t_end >= ? ORDER BY t_end DESC LIMIT 1",
                (detector, *cells, iso(not_before))).fetchone()
        return self._sensor_out(row)

    def get_sensor_event_by_key(self, dedupe_key: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM sensor_events WHERE dedupe_key=?",
                                   (dedupe_key,)).fetchone()
        return self._sensor_out(row)

    def save_sensor_event(self, event: dict, sensor: dict, corroborations: list[dict],
                          summary: dict) -> dict[str, int]:
        """Upsert a sensor-derived event: its sensor_events row (dedupe_key unique), then
        the events row, evidence rows and roll-up (see save_corroborations)."""
        now = iso(utcnow())
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO sensor_events VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(event_id) DO UPDATE SET t_start=excluded.t_start, "
                "t_end=excluded.t_end, details=excluded.details, "
                "updated_at=excluded.updated_at",
                (event["id"], sensor["detector"], sensor["dedupe_key"], sensor["cell"],
                 event["t_start"], event["t_end"],
                 json.dumps(sensor.get("details") or {}, default=str), now, now))
        return self.save_corroborations(corroborations, events=[{**event, "origin": "sensor"}],
                                        summaries=[summary])

    def stale_sensor_event_ids(self, versions: dict[str, int]) -> list[str]:
        """Sensor events written by an older detector version (``details.detector_version``
        below ``versions[detector]``; rows without one count as version 1)."""
        out: list[str] = []
        with self._lock:
            for detector, version in versions.items():
                out += [r[0] for r in self._db.execute(
                    "SELECT event_id FROM sensor_events WHERE detector = ? AND "
                    "COALESCE(json_extract(details, '$.detector_version'), 1) < ?",
                    (detector, int(version)))]
        return out

    def corroboration_stats(self) -> dict:
        with self._lock:
            kinds = self._db.execute(
                "SELECT kind, COUNT(*), COUNT(DISTINCT event_id) FROM corroborations "
                "GROUP BY kind").fetchall()
            corr = self._db.execute(
                "SELECT COUNT(*) FROM event_corroboration WHERE corroborated=1 AND event_id "
                "NOT IN (SELECT event_id FROM sensor_events)").fetchone()[0]
            det = self._db.execute(
                "SELECT detector, COUNT(*) FROM sensor_events GROUP BY detector").fetchall()
        by_det = {r[0]: r[1] for r in det}
        return {
            "corroborations_by_kind": {r[0]: r[1] for r in kinds},
            "events_with_evidence_by_kind": {r[0]: r[2] for r in kinds},
            "news_events_sensor_corroborated": corr,
            "sensor_derived": {"total": sum(by_det.values()), "by_detector": by_det},
        }

    def close(self) -> None:
        with self._lock:
            self._db.close()
