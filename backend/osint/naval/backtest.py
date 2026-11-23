"""Backtest the carrier model against USNI Fleet Tracker regions over stored history.

    python -m osint.naval.backtest --data-dir data --start 2026-11-19T12:00 \
        --end 2026-11-23T06:00 --step-h 6 [--hours 48] [--json out.json]

Reads the data dir READ-ONLY (DuckDB over the lake's parquet files, SQLite in ``mode=ro``);
the naval store is a temporary copy, so a backtest never touches the running app's state.
For each replay time the job is computed as if it were "now" (fusion WITHOUT the USNI prior,
as in /api/naval/eval) and scored with ``fusion.evaluate`` against the latest stored USNI
tracker. USNI gives coarse regions only and is published weekly, so read the numbers as a
sanity check, not ground truth. Also reports false-positive heuristics: hypotheses whose
centre is within ``coast_km`` of land or ``platform_km`` of an offshore flare site.

Only APIs that predate the air-track fit are used here, so the same file can replay an older
checkout of the package (``PYTHONPATH=<old>/backend``) for before/after comparisons."""

import argparse
import json
import shutil
import sqlite3
import statistics
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from osint.config import ROOT
from osint.core.geo import haversine_km
from osint.naval.config import NavalConfig
from osint.naval.geo import Geo
from osint.naval.job import CarrierJob
from osint.naval.store import NavalStore


class ReadOnlyLake:
    """``Lake.query`` lookalike over the parquet files (whole dataset views, read-only)."""

    def __init__(self, lake_dir: Path) -> None:
        import duckdb

        self.con = duckdb.connect()
        for name in ("positions", "documents", "observations"):
            files = (lake_dir / name).as_posix() + "/*/*/*.parquet"
            if any((lake_dir / name).glob("*/*/*.parquet")):
                self.con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet("
                                 f"'{files}', hive_partitioning=false, union_by_name=true)")

    def query(self, sql: str, params: list | None = None, *, start=None, end=None) -> list:
        return self.con.execute(sql, params or []).fetchall()


class ReadOnlyStore:
    def __init__(self, sqlite_path: Path) -> None:
        self.db = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row

    def get_tracks(self, ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 900):
            ch = ids[i:i + 900]
            for r in self.db.execute(
                    f"SELECT * FROM tracks WHERE track_id IN ({','.join('?' * len(ch))})", ch):
                d = dict(r)
                d["extra"] = json.loads(d.get("extra") or "{}")
                out[d["track_id"]] = d
        return out

    def get_cursor(self, name: str) -> dict | None:
        try:
            r = self.db.execute("SELECT value FROM cursors WHERE name=?", (name,)).fetchone()
        except sqlite3.Error:
            return None
        return json.loads(r[0]) if r else None

    def list_events(self, **_kw) -> list:
        return []


def _job(data_dir: Path, tmp: Path, cfg: NavalConfig) -> CarrierJob:
    src = data_dir / "naval.sqlite"
    dst = tmp / "naval.sqlite"
    if src.is_file():
        s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
        d = sqlite3.connect(dst)
        s.backup(d)
        s.close()
        d.close()
    geo = Geo.from_reference(data_dir / "reference")
    settings = SimpleNamespace(sqlite_path=data_dir / "osint.sqlite", data_dir=tmp)
    return CarrierJob(ReadOnlyLake(data_dir / "lake"), ReadOnlyStore(data_dir / "osint.sqlite"),
                      settings, cfg, NavalStore(dst), geo_factory=lambda: geo)


def replay(job: CarrierJob, nows: list[datetime], hours: int, *, coast_km: float = 50.0,
           platform_km: float = 25.0) -> list[dict]:
    geo = job._geo_factory()
    usni = job.nstore.get_meta("usni")
    rows = []
    for now in nows:
        res = job.compute(hours, now=now, persist=False)
        ev = res["eval"]
        hyps = res["hypotheses"]
        near_coast = [h for h in hyps if geo.coast_km(h["lat"], h["lon"]) < coast_km]
        plat = getattr(geo, "platforms", None)
        near_plat = [h for h in hyps if plat is not None and plat.count
                     and plat.nearest_km(h["lat"], h["lon"], platform_km)[1] is not None]
        dists = [r["distance_km"] for r in ev.get("entries", [])
                 if r.get("status") == "at_sea" and r.get("distance_km") is not None]
        rows.append({
            "now": now.isoformat(), "hypotheses": len(hyps),
            "confident": sum(h["confidence"] > 0.5 for h in hyps),
            "max_confidence": max((h["confidence"] for h in hyps), default=0.0),
            "recall_with_slack": ev.get("recall_with_slack"),
            "recall_within_radius": ev.get("recall_within_radius"),
            "median_dist_km": statistics.median(dists) if dists else None,
            "near_coast": len(near_coast), "near_platform": len(near_plat),
            "air_evidence": {k: v.get("evidence", 0) for k, v in res["indicator_status"].items()
                             if k in ("sea_terminating_flight", "cvw_aircraft", "orbit",
                                      "air_track")},
            "air_tracks": len(res.get("air_tracks", [])),
            "items": [{"lat": h["lat"], "lon": h["lon"], "radius_km": h["radius_km"],
                       "confidence": h["confidence"], "kinds": h["kinds"],
                       "basis": h.get("centre_basis"),
                       "nearest_usni_km": min((round(haversine_km(
                           h["lat"], h["lon"], e["lat"], e["lon"])) for e in
                           (usni or {}).get("entries", []) if e.get("lat") is not None),
                           default=None),
                       "top": [e["description"][:160] for e in h["evidence"][:3]]}
                      for h in hyps],
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    def mean(k):
        v = [r[k] for r in rows if r[k] is not None]
        return round(sum(v) / len(v), 3) if v else None
    return {"replays": len(rows), "mean_hypotheses": mean("hypotheses"),
            "mean_confident": mean("confident"), "mean_recall_with_slack": mean("recall_with_slack"),
            "mean_recall_within_radius": mean("recall_within_radius"),
            "mean_median_dist_km": mean("median_dist_km"),
            "hypotheses_near_coast": sum(r["near_coast"] for r in rows),
            "hypotheses_near_platform": sum(r["near_platform"] for r in rows)}


def _ts(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--config", type=Path, default=ROOT / "config" / "naval.yaml")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--step-h", type=float, default=6.0)
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args(argv)
    nows, t = [], _ts(a.start)
    while t <= _ts(a.end):
        nows.append(t)
        t += timedelta(hours=a.step_h)
    tmp = Path(tempfile.mkdtemp(prefix="naval-backtest-"))
    try:
        job = _job(a.data_dir, tmp, NavalConfig.load(a.config))
        rows = replay(job, nows, a.hours)
        job.nstore.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = {"summary": summarize(rows), "rows": rows}
    if a.json:
        a.json.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(json.dumps(out["summary"], indent=1))
    for r in rows:
        print(r["now"][:16], "hyps", r["hypotheses"], "conf>0.5", r["confident"],
              "max", r["max_confidence"], "recall", r["recall_with_slack"],
              "median km", r["median_dist_km"], "air", r["air_evidence"],
              "fits", r["air_tracks"])


if __name__ == "__main__":
    main()
