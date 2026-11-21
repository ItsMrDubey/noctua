"""Gold-set tooling: a stratified random sample of our events (with their documents and
attached GDELT rows) plus a sample of GDELT rows we rejected, for human labelling."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

from osint.benchmark.data import Sources
from osint.benchmark.scorecard import iso_db
from osint.benchmark.store import BenchmarkStore, iso

_STRATA_SQL = """
SELECT e.status, EXISTS (SELECT 1 FROM gdelt_decisions g WHERE g.event_id = e.id
                         AND g.decision = 'accepted') AS has_gdelt, COUNT(*)
FROM events e WHERE e.t_end >= ? AND e.origin != 'historical' GROUP BY 1, 2
"""
_PICK_SQL = """
SELECT e.id FROM events e WHERE e.t_end >= ? AND e.status = ?
  AND e.origin != 'historical'
  AND EXISTS (SELECT 1 FROM gdelt_decisions g WHERE g.event_id = e.id
              AND g.decision = 'accepted') = ?
ORDER BY e.rowid LIMIT 1 OFFSET ?
"""
_EVENT_COLS = ("id", "category", "title", "place_name", "country", "precision", "lat", "lon",
               "status", "confidence", "t_start", "t_end", "first_seen", "source_diversity")


def allocate(sizes: dict, n: int) -> dict:
    """Proportional allocation of ``n`` picks over strata (largest remainder), at least
    one per non-empty stratum while picks remain, never more than a stratum holds."""
    total = sum(sizes.values())
    if total == 0 or n <= 0:
        return {k: 0 for k in sizes}
    n = min(n, total)
    keys = sorted(sizes, key=lambda k: (-sizes[k], str(k)))
    alloc = {k: 0 for k in sizes}
    for k in keys:  # one each first
        if n - sum(alloc.values()) > 0 and sizes[k] > 0:
            alloc[k] = 1
    left = n - sum(alloc.values())
    if left > 0:
        quotas = {k: (sizes[k] - alloc[k]) * left / max(total - sum(alloc.values()), 1)
                  for k in keys}
        for k in keys:
            alloc[k] += min(int(quotas[k]), sizes[k] - alloc[k])
        rest = n - sum(alloc.values())
        for k in sorted(keys, key=lambda k: -(quotas[k] - int(quotas[k]))):
            if rest <= 0:
                break
            if alloc[k] < sizes[k]:
                alloc[k] += 1
                rest -= 1
        for k in keys:  # remainder still left (capped strata): fill wherever room is
            while rest > 0 and alloc[k] < sizes[k]:
                alloc[k] += 1
                rest -= 1
    return alloc


def gdelt_details(src: Sources, gids: list[str], start: datetime,
                   now: datetime) -> dict[str, dict]:
    if not gids or src.lake is None:
        return {}
    lo = start - timedelta(hours=src.cfg.decision_join_margin_h)
    marks = ",".join("?" * len(gids))
    rows = src.lake_query(
        f"SELECT origin_id, any_value(title), any_value(url), any_value(attrs), "
        f"min(epoch_ms(published_at)) FROM documents WHERE kind = 'gdelt_event' "
        f"AND published_at >= ? AND origin_id IN ({marks}) GROUP BY origin_id",
        [lo, *gids], start=lo, end=now)
    out = {}
    for gid, title, url, attrs, added_ms in rows:
        a = json.loads(attrs) if attrs else {}
        out[gid] = {"title": title, "url": url, "date_added": (
            iso(datetime.fromtimestamp(added_ms / 1000, tz=start.tzinfo))
            if added_ms is not None else None),
            "geo_type": a.get("geo_type"), "geo_type_name": a.get("geo_type_name"),
            "geo_name": a.get("geo_name"), "event_code": a.get("event_code"),
            "num_mentions": a.get("num_mentions"), "num_sources": a.get("num_sources")}
    return out


def build_sample(src: Sources, store: BenchmarkStore, *, start: datetime, now: datetime,
                 n: int, seed: int | None = None) -> dict:
    rng = random.Random(seed)
    s = iso_db(start)
    events: list[dict] = []
    rejected: list[dict] = []
    alloc: dict = {}
    with src.events_db() as db:
        if db is None:
            return {"as_of": iso(now), "window_start": iso(start), "events": [],
                    "gdelt_rejected": [], "strata": {}}
        sizes = {(st, bool(g)): c for st, g, c in db.execute(_STRATA_SQL, (s,))}
        alloc = allocate(sizes, n)
        ids: list[str] = []
        for (status, has_gdelt), k in alloc.items():
            for off in sorted(rng.sample(range(sizes[(status, has_gdelt)]), k)):
                row = db.execute(_PICK_SQL, (s, status, int(has_gdelt), off)).fetchone()
                if row:
                    ids.append(row[0])
        marks = ",".join("?" * len(ids))
        if ids:
            cols = ", ".join(_EVENT_COLS)
            evs = {r[0]: dict(zip(_EVENT_COLS, r, strict=True)) for r in db.execute(
                f"SELECT {cols} FROM events WHERE id IN ({marks})", ids)}
            docs: dict[str, list] = {}
            for eid, title, url, source, pub, role in db.execute(
                    f"SELECT event_id, title, url, source, published_at, role FROM "
                    f"event_documents WHERE event_id IN ({marks}) AND role != 'gdelt' "
                    f"ORDER BY published_at", ids):
                docs.setdefault(eid, []).append({"title": title, "url": url,
                                                 "source": source, "published_at": pub,
                                                 "role": role})
            acc: dict[str, list[str]] = {}
            for gid, eid in db.execute(
                    f"SELECT gdelt_event_id, event_id FROM gdelt_decisions WHERE "
                    f"decision = 'accepted' AND event_id IN ({marks})", ids):
                acc.setdefault(eid, []).append(gid)
            verdicts = dict(db.execute(
                f"SELECT event_id, verdict FROM event_verification WHERE event_id IN "
                f"({marks})", ids).fetchall())
            corr = dict(db.execute(
                f"SELECT event_id, corroborated FROM event_corroboration WHERE event_id IN "
                f"({marks})", ids).fetchall())
            for eid in ids:
                e = evs.get(eid)
                if e is None:
                    continue
                e["verdict"] = verdicts.get(eid)
                e["sensor_corroborated"] = bool(corr.get(eid))
                e["documents"] = docs.get(eid, [])
                e["gdelt_attached"] = [{"gdelt_event_id": g} for g in acc.get(eid, [])]
                events.append(e)
        # rejected GDELT rows, stratified by reason
        rsizes = dict(db.execute(
            "SELECT reason, COUNT(*) FROM gdelt_decisions WHERE decided_at >= ? AND "
            "decision = 'rejected' GROUP BY reason", (s,)).fetchall())
        for reason, k in allocate(rsizes, src.cfg.rejected_sample_n).items():
            for off in sorted(rng.sample(range(rsizes[reason]), k)):
                row = db.execute(
                    "SELECT gdelt_event_id, reason, category, decided_at FROM "
                    "gdelt_decisions WHERE decided_at >= ? AND decision = 'rejected' AND "
                    "reason = ? ORDER BY rowid LIMIT 1 OFFSET ?", (s, reason, off)).fetchone()
                if row:
                    rejected.append({"gdelt_event_id": row[0], "reason": row[1],
                                     "category": row[2], "decided_at": row[3]})
    gids = [g["gdelt_event_id"] for e in events for g in e["gdelt_attached"]]
    gids += [r["gdelt_event_id"] for r in rejected]
    details = gdelt_details(src, gids, start, now)
    for e in events:
        e["gdelt_attached"] = [{**g, **details.get(g["gdelt_event_id"], {})}
                               for g in e["gdelt_attached"]]
    rejected = [{**r, **details.get(r["gdelt_event_id"], {})} for r in rejected]
    labels = store.labels([e["id"] for e in events])
    for e in events:
        e["label"] = labels.get(e["id"])
    return {"as_of": iso(now), "window_start": iso(start), "n": len(events),
            "strata": {f"{st}|{'gdelt' if g else 'no_gdelt'}": k
                       for (st, g), k in alloc.items()},
            "events": events, "gdelt_rejected": rejected}
