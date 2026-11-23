"""Terror Watch store (data_dir/terror.sqlite). Written by the worker's TerrorJob, read by
the API. No article text is stored: structured results, short verbatim quotes, metadata."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

STORE_NAME = "terror.sqlite"
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_status (
  source_id TEXT PRIMARY KEY, last_attempt TEXT, last_ok TEXT, ok INTEGER, http_status INTEGER,
  error TEXT, items INTEGER, content_hash TEXT, etag TEXT, last_modified TEXT);
CREATE TABLE IF NOT EXISTS extractions (
  event_id TEXT PRIMARY KEY, doc_id TEXT, url TEXT, status TEXT NOT NULL, result TEXT,
  cost_usd REAL NOT NULL DEFAULT 0, latency_s REAL, model TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_calls (ts REAL NOT NULL, event_id TEXT, ok INTEGER NOT NULL,
  cost_usd REAL NOT NULL DEFAULT 0, latency_s REAL, model TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS llm_calls_ts ON llm_calls(ts);
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY, event_id TEXT NOT NULL, klass TEXT, status TEXT NOT NULL, country TEXT,
  lat REAL, lon REAL, precision TEXT, sort_time TEXT, first_reported_at TEXT, occurred_at TEXT,
  follow_up INTEGER NOT NULL DEFAULT 0, group_id TEXT, group_kind TEXT, attribution_level TEXT,
  attack_type TEXT, target_type TEXT, killed_low INTEGER, killed_high INTEGER,
  injured_low INTEGER, injured_high INTEGER, n_outlets INTEGER, confidence TEXT,
  merged_into TEXT, data TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS incidents_sort ON incidents(sort_time);
CREATE INDEX IF NOT EXISTS incidents_country ON incidents(country);
CREATE INDEX IF NOT EXISTS incidents_group ON incidents(group_id);
CREATE TABLE IF NOT EXISTS designations (
  list_id TEXT NOT NULL, ref TEXT NOT NULL, name TEXT NOT NULL, kind TEXT, status TEXT,
  listed_on TEXT, delisted_on TEXT, group_id TEXT, group_basis TEXT, first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL, removed_at TEXT, data TEXT NOT NULL, PRIMARY KEY (list_id, ref));
CREATE INDEX IF NOT EXISTS designations_group ON designations(group_id);
CREATE TABLE IF NOT EXISTS designation_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, list_id TEXT NOT NULL, ref TEXT NOT NULL, name TEXT,
  kind TEXT, change TEXT NOT NULL, official_date TEXT, detected_at TEXT NOT NULL,
  group_id TEXT, basis TEXT);
CREATE INDEX IF NOT EXISTS designation_changes_at ON designation_changes(detected_at);
CREATE TABLE IF NOT EXISTS threat_levels (
  country TEXT NOT NULL, authority TEXT NOT NULL, scope TEXT NOT NULL, level TEXT, rank INTEGER,
  scale_max INTEGER, as_of TEXT, quote TEXT, source_url TEXT, fetched_at TEXT NOT NULL,
  data TEXT NOT NULL, PRIMARY KEY (country, authority, scope));
CREATE TABLE IF NOT EXISTS threat_level_history (
  country TEXT NOT NULL, authority TEXT NOT NULL, scope TEXT NOT NULL, level TEXT, rank INTEGER,
  prev_level TEXT, as_of TEXT, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS advisories (
  country TEXT PRIMARY KEY, level INTEGER, label TEXT, terrorism INTEGER, risks TEXT, title TEXT,
  published TEXT, updated TEXT, url TEXT, quote TEXT, fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS advisory_history (
  country TEXT NOT NULL, level INTEGER, prev_level INTEGER, terrorism INTEGER, updated TEXT,
  observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS official_docs (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT, title TEXT, summary TEXT, url TEXT,
  published_at TEXT, first_seen TEXT NOT NULL, date_basis TEXT, basis TEXT, group_ids TEXT,
  countries TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS official_docs_pub ON official_docs(published_at);
CREATE TABLE IF NOT EXISTS ucdp_osv (
  month TEXT NOT NULL, country TEXT NOT NULL, actor TEXT NOT NULL, group_id TEXT, category TEXT,
  dataset TEXT NOT NULL, events INTEGER NOT NULL, deaths_best INTEGER NOT NULL,
  deaths_low INTEGER NOT NULL, deaths_high INTEGER NOT NULL, PRIMARY KEY (month, country, actor));
CREATE TABLE IF NOT EXISTS indicators (scope TEXT PRIMARY KEY, computed_at TEXT NOT NULL,
  data TEXT NOT NULL);
"""


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _dump(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str)


class TerrorStore:
    def __init__(self, path: Path, *, readonly: bool = False) -> None:
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

    def _q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ------------------------------------------------------------- meta
    def set_meta(self, key: str, value, now: datetime | None = None) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO meta VALUES (?,?,?)",
                             (key, _dump(value), iso(now or datetime.now(UTC))))

    def get_meta(self, key: str):
        r = self._q("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(r[0][0]) if r else None

    # ------------------------------------------------------------- sources
    def source_state(self, sid: str) -> dict | None:
        r = self._q("SELECT * FROM source_status WHERE source_id=?", (sid,))
        return dict(r[0]) if r else None

    def record_source(self, sid: str, *, now: datetime, ok: bool, http_status: int | None = None,
                      error: str | None = None, items: int | None = None,
                      content_hash: str | None = None, etag: str | None = None,
                      last_modified: str | None = None) -> None:
        prev = self.source_state(sid) or {}
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO source_status VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, iso(now), iso(now) if ok else prev.get("last_ok"), int(ok), http_status,
                 (error or "")[:300] or None, items if items is not None else prev.get("items"),
                 content_hash or prev.get("content_hash"), etag or prev.get("etag"),
                 last_modified or prev.get("last_modified")))

    def sources(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM source_status ORDER BY source_id")]

    # ------------------------------------------------------------- extractions
    def extraction(self, event_id: str) -> dict | None:
        r = self._q("SELECT * FROM extractions WHERE event_id=?", (event_id,))
        return _ext_out(r[0]) if r else None

    def extractions(self, ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rows = self._q(f"SELECT * FROM extractions WHERE event_id IN "
                           f"({','.join('?' * len(chunk))})", chunk)
            out.update({r["event_id"]: _ext_out(r) for r in rows})
        return out

    def save_extraction(self, event_id: str, *, now: datetime, status: str, doc_id=None,
                        url=None, result=None, cost_usd: float = 0.0, latency_s=None,
                        model=None, error=None, attempts: int = 1) -> None:
        prev = self.extraction(event_id)
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO extractions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, doc_id, url, status, _dump(result) if result is not None else None,
                 (prev or {}).get("cost_usd", 0.0) + cost_usd, latency_s, model, attempts,
                 (error or "")[:300] or None, (prev or {}).get("created_at") or iso(now),
                 iso(now)))

    def log_call(self, ts: float, *, event_id: str | None, ok: bool, cost_usd: float = 0.0,
                 latency_s: float | None = None, model: str | None = None,
                 error: str | None = None) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?)",
                             (ts, event_id, int(ok), cost_usd, latency_s, model,
                              (error or "")[:300] or None))

    def call_times_since(self, ts: float) -> list[float]:
        return [r[0] for r in self._q("SELECT ts FROM llm_calls WHERE ts >= ? ORDER BY ts",
                                      (ts,))]

    def cost_since(self, ts: float) -> dict:
        r = self._q("SELECT COUNT(*), COALESCE(SUM(cost_usd),0), COALESCE(SUM(ok),0) "
                    "FROM llm_calls WHERE ts >= ?", (ts,))[0]
        return {"calls": r[0], "ok": r[1 + 1], "cost_usd": round(float(r[1]), 6)}

    # ------------------------------------------------------------- incidents
    def replace_incidents(self, records: list[dict], now: datetime) -> None:
        """Upsert classified incidents (excluded ones too, for audit)."""
        with self._lock, self._db:
            for r in records:
                k, inj = (r.get("casualties") or {}).get("killed") or {}, \
                    (r.get("casualties") or {}).get("injured") or {}
                grp = r.get("group") or {}
                self._db.execute(
                    "INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                    "?,?,?,?,?,?,?,?,?)",
                    (r["id"], r["event_id"], r.get("klass"), r["status"], r.get("country"),
                     r.get("lat"), r.get("lon"), r.get("precision"), r.get("sort_time"),
                     r.get("first_reported_at"), r.get("occurred_at"),
                     int(bool(r.get("follow_up"))), grp.get("group_id"), grp.get("kind"),
                     (r.get("attribution") or {}).get("level"), r.get("attack_type"),
                     r.get("target_type"), k.get("low"), k.get("high"), inj.get("low"),
                     inj.get("high"), (r.get("outlets") or {}).get("n"), r.get("confidence"),
                     r.get("merged_into"), _dump(r), iso(now)))

    def delete_incidents(self, ids: list[str]) -> None:
        with self._lock, self._db:
            for i in ids:
                self._db.execute("DELETE FROM incidents WHERE id=?", (i,))

    def incident(self, iid: str) -> dict | None:
        r = self._q("SELECT data FROM incidents WHERE id=? OR event_id=?", (iid, iid))
        return json.loads(r[0][0]) if r else None

    def incidents(self, *, statuses=None, klass=None, country=None, group_id=None,
                  since: str | None = None, include_follow_up: bool = True,
                  include_merged: bool = False, limit: int = 2000) -> list[dict]:
        sql, args = "SELECT data FROM incidents WHERE 1=1", []
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        if klass:
            ks = [klass] if isinstance(klass, str) else list(klass)
            sql += f" AND klass IN ({','.join('?' * len(ks))})"
            args += ks
        if country:
            sql += " AND country=?"
            args.append(country)
        if group_id:
            sql += " AND group_id=?"
            args.append(group_id)
        if since:
            sql += " AND COALESCE(sort_time, first_reported_at) >= ?"
            args.append(since)
        if not include_follow_up:
            sql += " AND follow_up=0"
        if not include_merged:
            sql += " AND merged_into IS NULL"
        sql += " ORDER BY COALESCE(sort_time, first_reported_at) DESC LIMIT ?"
        args.append(limit)
        return [json.loads(r[0]) for r in self._q(sql, args)]

    def incidents_near(self, lat: float, lon: float, deg: float, *, since: str,
                       statuses=("confirmed", "reported", "possible")) -> list[dict]:
        """Live incidents inside a lat/lon box (callers filter by true distance)."""
        rows = self._q(
            f"SELECT data FROM incidents WHERE merged_into IS NULL AND status IN "
            f"({','.join('?' * len(statuses))}) AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
            "AND COALESCE(sort_time, first_reported_at) >= ?",
            (*statuses, lat - deg, lat + deg, lon - deg * 2, lon + deg * 2, since))
        return [json.loads(r[0]) for r in rows]

    def incident_ids(self) -> list[str]:
        return [r[0] for r in self._q("SELECT id FROM incidents")]

    def status_counts(self) -> dict:
        return {f"{r[0]}:{r[1]}": r[2] for r in self._q(
            "SELECT klass, status, COUNT(*) FROM incidents WHERE merged_into IS NULL "
            "GROUP BY 1, 2")}

    # ------------------------------------------------------------- designations
    def designations(self, *, list_id=None, group_id=None, kind=None, include_removed=False
                     ) -> list[dict]:
        sql, args = "SELECT * FROM designations WHERE 1=1", []
        for col, val in (("list_id", list_id), ("group_id", group_id), ("kind", kind)):
            if val:
                sql += f" AND {col}=?"
                args.append(val)
        if not include_removed:
            sql += " AND removed_at IS NULL"
        rows = self._q(sql + " ORDER BY list_id, name", args)
        out = []
        for r in rows:
            d = dict(r)
            out.append({**{k: v for k, v in d.items() if k != "data"}, **json.loads(d["data"])})
        return out

    def upsert_designations(self, list_ids: list[str], entries: list[dict], now: datetime,
                            *, first_load: bool) -> list[dict]:
        """Replace the entries of ``list_ids`` with ``entries``; returns the changes:
        additions (official listing date when the list states it), status changes
        (listed -> delisted) and removals (entry vanished from the list). On the first load
        of a list nothing is reported as new (the list is the baseline), except entries
        whose own official date is within the lookback the caller passes."""
        changes: list[dict] = []
        nowi = iso(now)
        with self._lock, self._db:
            existing = {}
            for lid in list_ids:
                for r in self._db.execute("SELECT * FROM designations WHERE list_id=?", (lid,)):
                    existing[(r["list_id"], r["ref"])] = dict(r)
            seen = set()
            for e in entries:
                key = (e["list_id"], e["ref"])
                seen.add(key)
                prev = existing.get(key)
                data = _dump({k: e.get(k) for k in ("aliases", "programmes", "remarks",
                                                     "linked_to", "source_url")})
                self._db.execute(
                    "INSERT OR REPLACE INTO designations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (e["list_id"], e["ref"], e["name"], e.get("kind"), e.get("status"),
                     e.get("listed_on"), e.get("delisted_on"), e.get("group_id"),
                     e.get("group_basis"), (prev or {}).get("first_seen") or nowi, nowi, None,
                     data))
                if prev is None and not first_load:
                    changes.append({**e, "change": "added",
                                    "official_date": e.get("listed_on")})
                elif prev is not None and prev.get("removed_at"):
                    changes.append({**e, "change": "re-added",
                                    "official_date": e.get("listed_on")})
                elif prev is not None and prev.get("status") != e.get("status"):
                    changes.append({**e, "change": e.get("status") or "changed",
                                    "official_date": e.get("delisted_on") or e.get("listed_on")})
            for key, prev in existing.items():
                if key not in seen and not prev.get("removed_at"):
                    self._db.execute("UPDATE designations SET removed_at=? WHERE list_id=? AND "
                                     "ref=?", (nowi, key[0], key[1]))
                    changes.append({"list_id": key[0], "ref": key[1], "name": prev["name"],
                                    "kind": prev.get("kind"), "group_id": prev.get("group_id"),
                                    "change": "removed", "official_date": None})
            for c in changes:
                self._db.execute(
                    "INSERT INTO designation_changes (list_id, ref, name, kind, change, "
                    "official_date, detected_at, group_id, basis) VALUES (?,?,?,?,?,?,?,?,?)",
                    (c["list_id"], c["ref"], c.get("name"), c.get("kind"), c["change"],
                     c.get("official_date"), nowi, c.get("group_id"),
                     "detected by comparing successive fetches of the official list"))
        return changes

    def designation_changes(self, since: str | None = None, limit: int = 500) -> list[dict]:
        sql, args = "SELECT * FROM designation_changes", []
        if since:
            sql += " WHERE COALESCE(official_date, detected_at) >= ?"
            args.append(since[:10])
        sql += " ORDER BY COALESCE(official_date, detected_at) DESC, id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._q(sql, args)]

    # ------------------------------------------------------------- threat levels
    def save_level(self, lv: dict, now: datetime) -> dict | None:
        """Store a threat level; returns a change record when the level differs from the
        last stored value (never on first sight)."""
        key = (lv["country"], lv["authority"], lv.get("scope") or "national")
        with self._lock, self._db:
            prev = self._db.execute("SELECT level, rank FROM threat_levels WHERE country=? AND "
                                    "authority=? AND scope=?", key).fetchone()
            self._db.execute(
                "INSERT OR REPLACE INTO threat_levels VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (*key, lv.get("level"), lv.get("rank"), lv.get("scale_max"), lv.get("as_of"),
                 lv.get("quote"), lv.get("source_url"), iso(now), _dump(lv)))
            if prev is not None and prev["level"] != lv.get("level"):
                self._db.execute("INSERT INTO threat_level_history VALUES (?,?,?,?,?,?,?,?)",
                                 (*key, lv.get("level"), lv.get("rank"), prev["level"],
                                  lv.get("as_of"), iso(now)))
                return {"country": key[0], "authority": key[1], "scope": key[2],
                        "from": prev["level"], "to": lv.get("level"), "as_of": lv.get("as_of")}
        return None

    def levels(self, country: str | None = None) -> list[dict]:
        sql, args = "SELECT data, fetched_at FROM threat_levels", []
        if country:
            sql += " WHERE country=?"
            args.append(country)
        return [{**json.loads(r[0]), "fetched_at": r[1]} for r in self._q(sql, args)]

    def level_history(self, country: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM threat_level_history", []
        if country:
            sql += " WHERE country=?"
            args.append(country)
        return [dict(r) for r in self._q(sql + " ORDER BY observed_at DESC", args)]

    def save_advisories(self, items: list[dict], now: datetime) -> list[dict]:
        changes = []
        with self._lock, self._db:
            for a in items:
                if not a.get("country"):
                    continue
                prev = self._db.execute("SELECT level, terrorism FROM advisories WHERE "
                                        "country=?", (a["country"],)).fetchone()
                self._db.execute(
                    "INSERT OR REPLACE INTO advisories VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (a["country"], a.get("level"), a.get("label"), int(bool(a.get("terrorism"))),
                     _dump(a.get("risks") or []), a.get("title"), a.get("published"),
                     a.get("updated"), a.get("url"), a.get("quote"), iso(now)))
                if prev is not None and (prev["level"] != a.get("level") or
                                         bool(prev["terrorism"]) != bool(a.get("terrorism"))):
                    self._db.execute("INSERT INTO advisory_history VALUES (?,?,?,?,?,?)",
                                     (a["country"], a.get("level"), prev["level"],
                                      int(bool(a.get("terrorism"))), a.get("updated"), iso(now)))
                    changes.append({"country": a["country"], "from": prev["level"],
                                    "to": a.get("level"), "terrorism": a.get("terrorism"),
                                    "updated": a.get("updated")})
        return changes

    def advisories(self, country: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM advisories", []
        if country:
            sql += " WHERE country=?"
            args.append(country)
        out = []
        for r in self._q(sql, args):
            d = dict(r)
            d["risks"] = json.loads(d["risks"] or "[]")
            d["terrorism"] = bool(d["terrorism"])
            out.append(d)
        return out

    def advisory_history(self, country: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM advisory_history", []
        if country:
            sql += " WHERE country=?"
            args.append(country)
        return [dict(r) for r in self._q(sql + " ORDER BY observed_at DESC", args)]

    # ------------------------------------------------------------- official docs
    def save_docs(self, docs: list[dict], now: datetime) -> int:
        new = 0
        with self._lock, self._db:
            for d in docs:
                prev = self._db.execute("SELECT first_seen FROM official_docs WHERE id=?",
                                        (d["id"],)).fetchone()
                new += prev is None
                self._db.execute(
                    "INSERT OR REPLACE INTO official_docs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (d["id"], d["source"], d.get("kind"), d.get("title"), d.get("summary"),
                     d.get("url"), d.get("published_at"),
                     prev["first_seen"] if prev else iso(now), d.get("date_basis"),
                     d.get("basis"), _dump(d.get("group_ids") or []),
                     _dump(d.get("countries") or []), _dump(d)))
        return new

    def docs(self, *, source=None, kind=None, since: str | None = None, limit: int = 300
             ) -> list[dict]:
        sql, args = "SELECT * FROM official_docs WHERE 1=1", []
        if source:
            srcs = [source] if isinstance(source, str) else list(source)
            sql += f" AND source IN ({','.join('?' * len(srcs))})"
            args += srcs
        if kind:
            kinds = [kind] if isinstance(kind, str) else list(kind)
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        if since:
            sql += " AND COALESCE(published_at, first_seen) >= ?"
            args.append(since)
        sql += " ORDER BY COALESCE(published_at, first_seen) DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self._q(sql, args):
            d = dict(r)
            d.pop("data", None)
            d["group_ids"] = json.loads(d["group_ids"] or "[]")
            d["countries"] = json.loads(d["countries"] or "[]")
            out.append(d)
        return out

    # ------------------------------------------------------------- UCDP baseline
    def replace_ucdp(self, rows: list[tuple], dataset_months: list[str] | None = None) -> None:
        """rows: (month, country, actor, group_id, category, dataset, events, best, low,
        high). Replaces all rows (the aggregate is small)."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM ucdp_osv")
            self._db.executemany("INSERT OR REPLACE INTO ucdp_osv VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 rows)

    def ucdp(self, *, country: str | None = None, group_id: str | None = None,
             designated_only: bool = False) -> list[dict]:
        sql, args = "SELECT * FROM ucdp_osv WHERE 1=1", []
        if country:
            sql += " AND country=?"
            args.append(country)
        if group_id:
            sql += " AND group_id=?"
            args.append(group_id)
        if designated_only:
            sql += " AND group_id IS NOT NULL AND category != 'criminal'"
        return [dict(r) for r in self._q(sql + " ORDER BY month", args)]

    def ucdp_count(self) -> int:
        return self._q("SELECT COUNT(*) FROM ucdp_osv")[0][0]

    # ------------------------------------------------------------- indicators
    def save_indicators(self, scope: str, data: dict, now: datetime) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO indicators VALUES (?,?,?)",
                             (scope, iso(now), _dump(data)))

    def indicators(self, scope: str) -> dict | None:
        r = self._q("SELECT data, computed_at FROM indicators WHERE scope=?", (scope,))
        return {**json.loads(r[0][0]), "computed_at": r[0][1]} if r else None

    def indicator_scopes(self) -> list[str]:
        return [r[0] for r in self._q("SELECT scope FROM indicators")]


def _ext_out(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["result"] = json.loads(d["result"]) if d.get("result") else None
    return d


def get_store(runtime) -> TerrorStore:
    store = getattr(runtime, "terror_store", None)
    if store is None:
        store = TerrorStore(Path(runtime.settings.data_dir) / STORE_NAME)
        runtime.terror_store = store
    return store
