"""data_dir/storylines.sqlite: storylines derived by the worker's StorylineJob, read by the API.

  storylines           one row per thread: stable id, title (+ basis), theatre, span by
                       best-known event time (+ basis), counts, places / actors / categories,
                       bounds, representative and origin event, a fingerprint of the content
  storyline_members    event id -> thread, with its role (+ basis), best-known time (basis,
                       precision, band) and the slim event fields the list and map need
  storyline_links      the accepted links inside a thread with their scored reasons
  storyline_redirects  ids of threads that were folded into another one (old -> new)
  meta                 small JSON job state (last pass, cost)

Ids are stable across passes: a re-derived thread keeps the id of the stored thread it
overlaps most (``assign_ids``); a new thread is named after its origin event.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from osint.core.geo import utcnow

STORE_NAME = "storylines.sqlite"
SCHEMA = """
CREATE TABLE IF NOT EXISTS storylines (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, title_basis TEXT NOT NULL,
  theatre TEXT NOT NULL, theatre_label TEXT NOT NULL,
  t_first TEXT NOT NULL, t_first_basis TEXT NOT NULL, t_last TEXT NOT NULL,
  first_report TEXT, last_report TEXT,
  n_events INTEGER NOT NULL, n_news INTEGER NOT NULL, n_sensor INTEGER NOT NULL,
  categories TEXT NOT NULL DEFAULT '[]', places TEXT NOT NULL DEFAULT '[]',
  actors TEXT NOT NULL DEFAULT '[]', countries TEXT NOT NULL DEFAULT '[]',
  roles TEXT NOT NULL DEFAULT '{}',
  min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL, lat REAL, lon REAL,
  origin_event_id TEXT NOT NULL, representative_event_id TEXT NOT NULL,
  top_status TEXT, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS storylines_t_last ON storylines(t_last);
CREATE TABLE IF NOT EXISTS storyline_members (
  storyline_id TEXT NOT NULL, event_id TEXT NOT NULL, role TEXT NOT NULL,
  role_basis TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL, category TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '', t TEXT NOT NULL, t_basis TEXT NOT NULL,
  t_precision TEXT, t_lo TEXT, t_hi TEXT, first_report TEXT, t_end TEXT,
  lat REAL, lon REAL, precision TEXT, place_name TEXT, country TEXT, status TEXT,
  mentions INTEGER NOT NULL DEFAULT 0, sources INTEGER NOT NULL DEFAULT 0, detector TEXT,
  PRIMARY KEY (storyline_id, event_id));
CREATE INDEX IF NOT EXISTS storyline_members_event ON storyline_members(event_id);
CREATE TABLE IF NOT EXISTS storyline_links (
  storyline_id TEXT NOT NULL, a TEXT NOT NULL, b TEXT NOT NULL, score REAL NOT NULL,
  reasons TEXT NOT NULL DEFAULT '[]', PRIMARY KEY (storyline_id, a, b));
CREATE TABLE IF NOT EXISTS storyline_redirects (
  old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
"""
JSON_COLS = ("categories", "places", "actors", "countries", "roles")
MEMBER_COLS = ("storyline_id", "event_id", "role", "role_basis", "origin", "category", "title",
               "t", "t_basis", "t_precision", "t_lo", "t_hi", "first_report", "t_end", "lat",
               "lon", "precision", "place_name", "country", "status", "mentions", "sources",
               "detector")
STORY_COLS = ("id", "title", "title_basis", "theatre", "theatre_label", "t_first",
              "t_first_basis", "t_last", "first_report", "last_report", "n_events", "n_news",
              "n_sensor", "categories", "places", "actors", "countries", "roles", "min_lat",
              "min_lon", "max_lat", "max_lon", "lat", "lon", "origin_event_id",
              "representative_event_id", "top_status", "fingerprint", "created_at",
              "updated_at")


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _story_out(row: sqlite3.Row) -> dict:
    d = dict(row)
    for c in JSON_COLS:
        d[c] = json.loads(d[c] or ("{}" if c == "roles" else "[]"))
    return d


class StorylineStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
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

    # ------------------------------------------------------------------ writes

    def memberships(self) -> dict[str, set[str]]:
        """storyline id -> its member event ids (id assignment input)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT storyline_id, event_id FROM storyline_members").fetchall()
        out: dict[str, set[str]] = {}
        for sid, eid in rows:
            out.setdefault(sid, set()).add(eid)
        return out

    def fingerprints(self) -> dict[str, str]:
        with self._lock:
            rows = self._db.execute("SELECT id, fingerprint FROM storylines").fetchall()
        return {r[0]: r[1] for r in rows}

    def sync(self, stories: list[dict], redirects: dict[str, str] | None = None
             ) -> dict[str, int]:
        """Make the stored set equal ``stories`` (each {story columns..., members: [...],
        links: [...]}), writing only rows whose fingerprint changed; ids not in ``stories``
        are deleted, and ``redirects`` (old id -> new id) recorded. One transaction."""
        now = iso(utcnow())
        known = self.fingerprints()
        keep = {s["id"] for s in stories}
        out = {"written": 0, "unchanged": 0, "deleted": 0}
        with self._lock, self._db:
            db = self._db
            for s in stories:
                if known.get(s["id"]) == s["fingerprint"]:
                    out["unchanged"] += 1
                    continue
                created = db.execute("SELECT created_at FROM storylines WHERE id=?",
                                     (s["id"],)).fetchone()
                row = {**s, "created_at": created[0] if created else now, "updated_at": now}
                db.execute(f"INSERT OR REPLACE INTO storylines ({', '.join(STORY_COLS)}) "
                           f"VALUES ({','.join('?' * len(STORY_COLS))})",
                           [json.dumps(row[c], ensure_ascii=False) if c in JSON_COLS
                            else row.get(c) for c in STORY_COLS])
                db.execute("DELETE FROM storyline_members WHERE storyline_id=?", (s["id"],))
                db.execute("DELETE FROM storyline_links WHERE storyline_id=?", (s["id"],))
                db.executemany(
                    f"INSERT INTO storyline_members ({', '.join(MEMBER_COLS)}) VALUES "
                    f"({','.join('?' * len(MEMBER_COLS))})",
                    [[s["id"] if c == "storyline_id" else m.get(c) for c in MEMBER_COLS]
                     for m in s["members"]])
                db.executemany("INSERT OR REPLACE INTO storyline_links VALUES (?,?,?,?,?)",
                               [(s["id"], lk["a"], lk["b"], lk["score"],
                                 json.dumps(lk["reasons"], ensure_ascii=False))
                                for lk in s["links"]])
                out["written"] += 1
            gone = [sid for sid in known if sid not in keep]
            for sid in gone:
                for table, col in (("storylines", "id"), ("storyline_members", "storyline_id"),
                                   ("storyline_links", "storyline_id")):
                    db.execute(f"DELETE FROM {table} WHERE {col}=?", (sid,))
            out["deleted"] = len(gone)
            for old, new in (redirects or {}).items():
                db.execute("UPDATE storyline_redirects SET new_id=? WHERE new_id=?", (new, old))
                db.execute("INSERT OR REPLACE INTO storyline_redirects VALUES (?,?,?)",
                           (old, new, now))
            if keep:  # a thread that exists again is no longer redirected
                db.executemany("DELETE FROM storyline_redirects WHERE old_id=?",
                               [(k,) for k in keep])
        return out

    # ------------------------------------------------------------------ reads

    def resolve(self, storyline_id: str) -> str:
        with self._lock:
            row = self._db.execute("SELECT new_id FROM storyline_redirects WHERE old_id=?",
                                   (storyline_id,)).fetchone()
        return row[0] if row else storyline_id

    def list(self, *, bbox: tuple[float, float, float, float] | None = None,
             since: datetime | None = None, until: datetime | None = None,
             category: str | None = None, theatre: str | None = None, q: str | None = None,
             min_events: int = 2, limit: int = 100) -> list[dict]:
        """Storylines, latest activity (best-known event time) first. ``since`` / ``until``
        select threads whose span overlaps the range; bbox = (west, south, east, north)
        intersects the thread's bounds (west > east wraps the antimeridian)."""
        where = ["n_events >= ?"]
        params: list[object] = [int(min_events)]
        if since is not None:
            where.append("t_last >= ?")
            params.append(iso(since))
        if until is not None:
            where.append("t_first <= ?")
            params.append(iso(until))
        if bbox is not None:
            w, s, e, n = bbox
            where.append("max_lat >= ? AND min_lat <= ?")
            params += [s, n]
            if w <= e:
                where.append("max_lon >= ? AND min_lon <= ?")
            else:
                where.append("(max_lon >= ? OR min_lon <= ?)")
            params += [w, e]
        cats = [c.strip() for c in (category or "").split(",") if c.strip()]
        if cats:
            where.append("EXISTS (SELECT 1 FROM json_each(categories) WHERE value IN ("
                         + ",".join("?" * len(cats)) + "))")
            params += cats
        if theatre:
            where.append("theatre = ?")
            params.append(theatre)
        if q:
            like = "%" + q.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
            where.append("(title LIKE ? ESCAPE '!' OR places LIKE ? ESCAPE '!' OR "
                         "theatre_label LIKE ? ESCAPE '!')")
            params += [like, like, like]
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM storylines WHERE {' AND '.join(where)} "
                "ORDER BY t_last DESC, id LIMIT ?", params).fetchall()
        return [_story_out(r) for r in rows]

    def get(self, storyline_id: str) -> dict | None:
        """Full thread: row, members (by best-known time) and links. A redirected id
        resolves (``redirected_from`` set)."""
        sid = self.resolve(storyline_id)
        with self._lock:
            row = self._db.execute("SELECT * FROM storylines WHERE id=?", (sid,)).fetchone()
            if row is None:
                return None
            members = self._db.execute(
                "SELECT * FROM storyline_members WHERE storyline_id=? ORDER BY t, event_id",
                (sid,)).fetchall()
            links = self._db.execute(
                "SELECT a, b, score, reasons FROM storyline_links WHERE storyline_id=? "
                "ORDER BY score DESC", (sid,)).fetchall()
        out = _story_out(row)
        out["members"] = [{k: v for k, v in dict(m).items() if k != "storyline_id"}
                          for m in members]
        out["links"] = [{"a": a, "b": b, "score": sc, "reasons": json.loads(rs)}
                        for a, b, sc, rs in links]
        if sid != storyline_id:
            out["redirected_from"] = storyline_id
        return out

    def for_events(self, event_ids: list[str]) -> dict[str, dict]:
        """event id -> {storyline_id, role, title, n_events, t_first, t_last, theatre_label}
        for events that are members of a storyline."""
        out: dict[str, dict] = {}
        for i in range(0, len(event_ids), 800):
            chunk = event_ids[i:i + 800]
            with self._lock:
                rows = self._db.execute(
                    "SELECT m.event_id, m.role, m.role_basis, s.id, s.title, s.n_events, "
                    "s.t_first, s.t_first_basis, s.t_last, s.theatre_label, s.theatre "
                    "FROM storyline_members m JOIN storylines s ON s.id = m.storyline_id "
                    f"WHERE m.event_id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
            for r in rows:
                out[r[0]] = {"storyline_id": r[3], "role": r[1], "role_basis": r[2],
                             "title": r[4], "n_events": r[5], "t_first": r[6],
                             "t_first_basis": r[7], "t_last": r[8], "theatre_label": r[9],
                             "theatre": r[10]}
        return out

    def theatres(self, since: datetime | None = None) -> list[dict]:
        """Theatres (country x category family) with thread counts and latest activity."""
        where, params = "", []
        if since is not None:
            where, params = "WHERE t_last >= ?", [iso(since)]
        with self._lock:
            rows = self._db.execute(
                "SELECT theatre, theatre_label, COUNT(*), SUM(n_events), MAX(t_last), "
                f"MIN(t_first) FROM storylines {where} GROUP BY theatre, theatre_label "
                "ORDER BY MAX(t_last) DESC", params).fetchall()
        return [{"theatre": r[0], "label": r[1], "storylines": r[2], "events": r[3],
                 "t_last": r[4], "t_first": r[5]} for r in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM storylines").fetchone()[0])


def get_store(runtime) -> StorylineStore:
    """The runtime's storyline store, created on first use (shared by the job and the API)."""
    store = getattr(runtime, "storyline_store", None)
    if store is None:
        store = StorylineStore(runtime.settings.data_dir / STORE_NAME)
        runtime.storyline_store = store
    return store
