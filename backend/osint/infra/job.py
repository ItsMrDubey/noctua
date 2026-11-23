"""WatchJob (worker): flags live AIS / ADS-B tracks that match a watchlist and vessels that
loiter near submarine cables / offshore pipelines, and keeps the infrastructure datasets
fresh. Results go to ``data/infra_watch.sqlite`` so the API process (split mode) and the
single-process runtime read them the same way.

Alerts ("alert events") are raised once per (track, reason) and again only after
``realert_h`` hours without a sighting. CPU work runs in a worker thread.
"""

import asyncio
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from osint.core.geo import utcnow
from osint.infra import datasets
from osint.infra.geo import PointGrid, SegmentGrid
from osint.infra.loader import Infra, reset_infra
from osint.infra.watchlist import SEV_ORDER, Matcher, SanctionsIndex, UserWatchlist
from osint.reference.loader import default_dir

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
  track_id TEXT NOT NULL, severity TEXT NOT NULL, title TEXT NOT NULL, sub TEXT NOT NULL,
  lat REAL, lon REAL, detail TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts(ts);
CREATE TABLE IF NOT EXISTS sightings (
  track_id TEXT NOT NULL, reason TEXT NOT NULL, first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL, PRIMARY KEY (track_id, reason));
CREATE TABLE IF NOT EXISTS active (
  track_id TEXT PRIMARY KEY, severity TEXT NOT NULL, name TEXT, lat REAL, lon REAL,
  ts TEXT NOT NULL, hits TEXT NOT NULL);
"""
DEFAULTS = {
    "job": {"interval_s": 60, "initial_delay_s": 45, "auto_refresh": True, "refresh_check_h": 6,
            "max_fix_age_min": 30},
    "watchlist": {"topics": ["sanction", "mare.shadow", "poi", "sanction.linked"],
                  "name_matches": True, "alert_weak": False, "realert_h": 24},
    "proximity": {"enabled": True, "max_kts": 3.0, "km": 2.0, "minutes": 30,
                  "targets": ["cables", "pipelines_offshore"], "port_exclusion_km": 8.0,
                  "dark_min": 60, "coast_km": 3.0, "require_transit": True, "transit_kts": 6.0,
                  "transit_window_h": 6, "exclude_categories": [
                      "Fishing", "Pilot Vessel", "Port Tender", "Pleasure Craft", "Sailing",
                      "Search & Rescue", "Law Enforcement", "Tug", "Towing", "Diving Ops",
                      "Medical Transport", "Passenger", "High Speed Craft"]},
    "retention_days": 30,
}
META_TTL_S = 600.0
META_EMPTY_TTL_S = 120.0  # tracks without static data yet (AIS type 5 arrives later)


def load_config(path: Path) -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if path.is_file():
        with path.open(encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class WatchStore:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self._lock = threading.Lock()
        if read_only:
            self._db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True,
                                       check_same_thread=False)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
        self._db.row_factory = sqlite3.Row

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def set_meta(self, key: str, value: object) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def get_meta(self, key: str) -> object | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def sight(self, track_id: str, reason: str, now: datetime, realert: timedelta) -> bool:
        """Record a sighting; True when it is a first sighting (or the first after a gap of
        ``realert``) and so deserves an alert."""
        ts = _iso(now)
        with self._lock, self._db:
            row = self._db.execute("SELECT last_seen FROM sightings WHERE track_id=? AND reason=?",
                                   (track_id, reason)).fetchone()
            if row is None:
                self._db.execute("INSERT INTO sightings VALUES (?,?,?,?)", (track_id, reason, ts, ts))
                return True
            self._db.execute("UPDATE sightings SET last_seen=? WHERE track_id=? AND reason=?",
                             (ts, track_id, reason))
            return datetime.fromisoformat(row[0]) < now - realert

    def add_alert(self, a: dict) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO alerts (ts, kind, track_id, severity, title, sub, lat, lon, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (a["ts"], a["kind"], a["track_id"], a["severity"], a["title"], a.get("sub") or "",
                 a.get("lat"), a.get("lon"), json.dumps(a.get("detail") or {})))
            return int(cur.lastrowid)

    def alerts(self, since_id: int = 0, limit: int = 200, since: datetime | None = None) -> list[dict]:
        sql, args = "SELECT * FROM alerts WHERE id > ?", [since_id]
        if since is not None:
            sql += " AND ts >= ?"
            args.append(_iso(since))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"] or "{}")
            out.append(d)
        return out

    def set_active(self, rows: list[dict]) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM active")
            self._db.executemany(
                "INSERT INTO active VALUES (?,?,?,?,?,?,?)",
                [(r["track_id"], r["severity"], r.get("name"), r.get("lat"), r.get("lon"), r["ts"],
                  json.dumps(r["hits"])) for r in rows])

    def active(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM active").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["hits"] = json.loads(d["hits"])
            out.append(d)
        return out

    def prune(self, before: datetime) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM alerts WHERE ts < ?", (_iso(before),))
            self._db.execute("DELETE FROM sightings WHERE last_seen < ?", (_iso(before),))


def coast_points(path: Path) -> list[tuple[float, float]]:
    """(lat, lon) of every Natural Earth country polygon vertex: a cheap "near land" test."""
    try:
        gj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("watch: no coastline (%s); coast exclusion off", path)
        return []
    out = []
    for f in gj.get("features") or []:
        g = f.get("geometry") or {}
        polys = g.get("coordinates") or []
        if g.get("type") == "Polygon":
            polys = [polys]
        for poly in polys:
            for ring in poly:
                out.extend((y, x) for x, y, *_ in ring)
    return out


def store_path(data_dir: Path) -> Path:
    return data_dir / "infra_watch.sqlite"


class Proximity:
    """Loiter / dark detector over a grid of cable and offshore-pipeline segments.

    A vessel alerts when it slows to <= max_kts within ``km`` of an indexed line for
    ``minutes`` after it was seen under way (> transit_kts) within ``transit_window_h``:
    a ship that has sat at an anchorage since before we first saw it is not a new event.
    Positions near ports, coasts (Natural Earth vertices) or excluded vessel categories are
    ignored. "Dark": a loitering vessel stops reporting for ``dark_min`` while the AIS feed
    itself is alive."""

    def __init__(self, cfg: dict, infra: Infra, ports: list[dict],
                 coast: list[tuple[float, float]] | None = None) -> None:
        self.cfg = cfg
        self.km = float(cfg["km"])
        self.grid = SegmentGrid(cell_deg=0.25, buffer_km=max(self.km, 0.5))
        targets = set(cfg.get("targets") or [])
        if "cables" in targets:
            for c in infra.cables:
                owner = {"type": "cable", "id": c["id"], "name": c["name"]}
                for ln in c["lines"]:
                    self.grid.add_line(ln, owner)
        if targets & {"pipelines", "pipelines_offshore"}:
            for p in infra.pipelines:
                if "pipelines" in targets or p.get("offshore"):
                    self.grid.add_line(p["line"], {"type": "pipeline", "id": p["id"],
                                                   "name": p["name"] or "unnamed pipeline",
                                                   "substance": p.get("substance")})
        self.ports = PointGrid(0.5)
        for p in ports:
            self.ports.add(p["lat"], p["lon"], p)
        self.coast = PointGrid(0.25)
        for lat, lon in coast or []:
            self.coast.add(lat, lon, None)
        self.excl = {c.lower() for c in cfg.get("exclude_categories") or []}
        self.state: dict[str, dict] = {}
        self.moving: dict[str, datetime] = {}  # last fix under way (> transit_kts)

    def _excluded(self, p, cat: str) -> bool:
        cfg = self.cfg
        return (cat in self.excl
                or self.ports.near(p.lat, p.lon, float(cfg["port_exclusion_km"])) is not None
                or (float(cfg.get("coast_km") or 0) > 0
                    and self.coast.near(p.lat, p.lon, float(cfg["coast_km"])) is not None))

    def forget(self, keep: set[str]) -> None:
        for d in (self.state, self.moving):
            for tid in [t for t in d if t not in keep]:
                del d[tid]

    def step(self, p, meta: dict | None, now: datetime, feed_alive: bool) -> list[dict]:
        """Update one sea track; returns alert dicts (kind 'proximity' | 'dark')."""
        cfg = self.cfg
        tid = p.track_id
        st = self.state.get(tid)
        cat = ((meta or {}).get("category") or "").lower()
        dur = timedelta(minutes=float(cfg["minutes"]))
        if st is not None and p.ts <= st["last_ts"]:  # no new fix since the last pass
            silent = now - st["last_ts"]
            if (feed_alive and st["transit"] and not st.get("dark")
                    and st["last_ts"] - st["since"] >= dur / 2
                    and silent >= timedelta(minutes=float(cfg["dark_min"]))):
                st["dark"] = True
                return [self._alert("dark", p, meta, st, now, silent)]
            return []
        if p.speed_kts is not None and p.speed_kts > float(cfg.get("transit_kts", 6.0)):
            self.moving[tid] = p.ts
        slow = p.speed_kts is not None and p.speed_kts <= float(cfg["max_kts"])
        near = self.grid.near(p.lat, p.lon, self.km) if slow else None
        if near is None or self._excluded(p, cat):
            self.state.pop(tid, None)
            return []
        owner, d = near
        if st is None or st["owner"]["id"] != owner["id"]:
            under_way = self.moving.get(tid)
            window = timedelta(hours=float(cfg.get("transit_window_h", 6)))
            transit = not cfg.get("require_transit", True) or (
                under_way is not None and p.ts - under_way <= window)
            st = self.state[tid] = {"owner": owner, "since": p.ts, "km": d, "alerted": False,
                                    "transit": transit}
        st.update(last_ts=p.ts, km=min(st["km"], d), lat=p.lat, lon=p.lon, dark=False,
                  max_kts=max(st.get("max_kts") or 0.0, p.speed_kts or 0.0))
        if st["transit"] and not st["alerted"] and p.ts - st["since"] >= dur:
            st["alerted"] = True
            return [self._alert("proximity", p, meta, st, now, p.ts - st["since"])]
        return []

    def _alert(self, kind: str, p, meta: dict | None, st: dict, now: datetime,
               span: timedelta) -> dict:
        o = st["owner"]
        what = "submarine cable" if o["type"] == "cable" else f"{o.get('substance') or ''} pipeline".strip()
        name = (meta or {}).get("name") or p.track_id.removeprefix("sea:")
        mins = int(span.total_seconds() // 60)
        if kind == "dark":
            title = f"AIS dark near {what} · {name}"
            reason = (f"Stopped transmitting {mins} min ago after loitering within "
                      f"{st['km']:.1f} km of {o['name']}")
        else:
            title = f"Loitering near {what} · {name}"
            reason = (f"<= {self.cfg['max_kts']} kn for {mins} min within {st['km']:.1f} km of "
                      f"{o['name']}")
        return {"ts": _iso(now), "kind": kind, "track_id": p.track_id,
                "severity": "warn", "title": title, "sub": reason, "lat": p.lat, "lon": p.lon,
                "detail": {"infra": o, "km": round(st["km"], 2), "minutes": mins,
                           "max_kts": st.get("max_kts"), "reason": reason,
                           "category": (meta or {}).get("category")}}


class WatchJob:
    def __init__(self, runtime, cfg: dict, store: WatchStore, ref_dir: Path | None = None) -> None:
        self.rt = runtime
        self.cfg = cfg
        self.store = store
        self.ref_dir = ref_dir or default_dir()
        self.matcher: Matcher | None = None
        self.prox: Proximity | None = None
        self.user = UserWatchlist(runtime.settings.config_dir / "watchlist.yaml")
        self._meta: dict[str, tuple[dict, float]] = {}
        self._refresh_at = 0.0
        self.passes = 0
        self.last_error: str | None = None
        self.last: dict = {}

    # ------------------------------------------------------------------ setup

    def build(self) -> None:
        wl = self.cfg["watchlist"]
        sanctions = SanctionsIndex(self.ref_dir / "os_maritime.csv",
                                   self.ref_dir / "os_targets_va.csv", set(wl["topics"]))
        self.matcher = Matcher(sanctions if sanctions.entries else None, self.user,
                               name_matches=bool(wl["name_matches"]))
        if self.cfg["proximity"].get("enabled"):
            from osint.reference.loader import Reference

            ports = Reference(self.ref_dir).ports
            self.prox = Proximity(self.cfg["proximity"], Infra(self.ref_dir), ports,
                                  coast_points(self.ref_dir / "ne_50m_admin_0_countries.geojson"))
            log.info("watch: proximity grid %d segments, %d ports", len(self.prox.grid), len(ports))

    def maybe_refresh(self) -> bool:
        """Re-download stale datasets (at most every refresh_check_h); True if any changed."""
        job = self.cfg["job"]
        if not job.get("auto_refresh") or time.time() < self._refresh_at:
            return False
        self._refresh_at = time.time() + float(job["refresh_check_h"]) * 3600
        res = datasets.refresh(self.ref_dir)
        changed = [k for k, v in res.items() if v.get("ok") and not v.get("skipped")]
        failed = {k: v["error"] for k, v in res.items() if not v.get("ok")}
        self.store.set_meta("refresh", {"at": _iso(utcnow()), "changed": changed, "failed": failed})
        if changed:
            reset_infra()
        return bool(changed)

    # ------------------------------------------------------------------ pass

    def _metas(self, ids: list[str]) -> dict[str, dict]:
        now = time.monotonic()
        stale = [i for i in ids if (m := self._meta.get(i)) is None or now - m[1] > META_TTL_S
                 or (not m[0] and now - m[1] > META_EMPTY_TTL_S)]
        if stale:
            got = self.rt.store.get_tracks(stale)
            for i in stale:
                self._meta[i] = (got.get(i) or {}, now)
        live = set(ids)
        if len(self._meta) > 2 * len(live) + 1000:
            self._meta = {k: v for k, v in self._meta.items() if k in live}
        return {i: self._meta[i][0] for i in ids}

    def run_pass(self, now: datetime | None = None) -> dict:
        now = now or utcnow()
        if self.matcher is None:
            self.build()
        self.user.reload()
        job, wl = self.cfg["job"], self.cfg["watchlist"]
        with self.rt.ingestor._lock:
            live = list(self.rt.ingestor.live.values())
        cutoff = now - timedelta(minutes=float(job["max_fix_age_min"]))
        fresh = [p for p in live if p.ts >= cutoff or (p.kind == "sea" and p.ts >= now - timedelta(hours=6))]
        metas = self._metas([p.track_id for p in fresh])
        realert = timedelta(hours=float(wl["realert_h"]))
        active, new_alerts = [], 0
        for p in fresh:
            if p.ts < cutoff:
                continue
            hits = self.matcher.match(p.track_id, metas.get(p.track_id))
            if not hits:
                continue
            top = hits[0]
            sev = min((h["entry"]["severity"] for h in hits), key=SEV_ORDER.__getitem__)
            if top["strength"] == "weak":
                sev = "info"
            m = metas.get(p.track_id) or {}
            name = m.get("name") or p.track_id.split(":", 1)[1]
            active.append({"track_id": p.track_id, "severity": sev, "name": name, "lat": p.lat,
                           "lon": p.lon, "ts": _iso(p.ts), "hits": hits})
            if top["strength"] == "weak" and not wl.get("alert_weak"):
                continue
            if self.store.sight(p.track_id, "watch:" + top["entry"]["id"], now, realert):
                e = top["entry"]
                label = e.get("label") or ", ".join(e.get("risk") or []) or "Watchlist"
                self.store.add_alert({
                    "ts": _iso(now), "kind": "watchlist", "track_id": p.track_id, "severity": sev,
                    "title": f"{label} · {name}",
                    "sub": " · ".join(x for x in [
                        "matched " + top["matched_on"].upper(), e.get("name"),
                        ", ".join((e.get("programs") or [])[:3])] if x),
                    "lat": p.lat, "lon": p.lon, "detail": {"hits": hits}})
                new_alerts += 1
        self.store.set_active(active)
        prox_alerts = 0
        if self.prox is not None:
            sea = [p for p in fresh if p.kind == "sea"]
            newest = max((p.ts for p in sea), default=None)
            feed_alive = newest is not None and now - newest < timedelta(minutes=10)
            for p in sea:
                for a in self.prox.step(p, metas.get(p.track_id), now, feed_alive):
                    if self.store.sight(a["track_id"], f"{a['kind']}:{a['detail']['infra']['id']}",
                                        now, realert):
                        self.store.add_alert(a)
                        prox_alerts += 1
            self.prox.forget({p.track_id for p in sea})
        self.passes += 1
        self.last = {"at": _iso(now), "live": len(fresh), "matched": len(active),
                     "watch_alerts": new_alerts, "proximity_alerts": prox_alerts,
                     "loitering": sum(1 for s in (self.prox.state.values() if self.prox else [])
                                      if s.get("since")),
                     "index": self.matcher.sanctions.summary() if self.matcher.sanctions else None,
                     "user_entries": len(self.user.entries), "user_error": self.user.error,
                     "proximity_segments": len(self.prox.grid) if self.prox else 0}
        self.store.set_meta("status", {**self.last, "passes": self.passes, "last_error": None})
        return self.last

    async def run(self) -> None:
        job = self.cfg["job"]
        await asyncio.sleep(float(job["initial_delay_s"]))
        while True:
            try:
                if await asyncio.to_thread(self.maybe_refresh):
                    self.matcher = None  # rebuild indexes from the new files
                await asyncio.to_thread(self.run_pass)
                self.last_error = None
                await asyncio.to_thread(self.store.prune,
                                        utcnow() - timedelta(days=float(self.cfg["retention_days"])))
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("watch pass failed")
                try:
                    self.store.set_meta("status", {**self.last, "passes": self.passes,
                                                   "last_error": self.last_error})
                except Exception:
                    log.exception("watch status write failed")
            await asyncio.sleep(float(job["interval_s"]))


def start(runtime) -> asyncio.Task:
    """Create the watch job (as runtime.watch_job) and start its loop as a task."""
    cfg = load_config(runtime.settings.config_dir / "infra.yaml")
    job = WatchJob(runtime, cfg, WatchStore(store_path(runtime.settings.data_dir)))
    runtime.watch_job = job
    task = asyncio.create_task(job.run(), name="infra_watch")
    runtime._tasks.append(task)
    return task
