"""Maintenance: re-run triage over documents already in the lake, then optionally rebuild
events, so gazetteer / triage fixes apply retroactively.

    python -m osint.pipeline.retriage [--days N] [--rebuild-events] [--dry-run]

Safe while the server runs: partitions are rewritten one at a time and the lake write lock is
held only for each file swap (``Lake.rewrite_partition``). The running server's EventBuilder
keeps an in-memory index, so RESTART THE SERVER after ``--rebuild-events``.
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from typing import Any

import msgspec

from osint.config import Settings
from osint.core.geo import utcnow
from osint.core.models import Document
from osint.pipeline.triage import Triage
from osint.store.lake import SCHEMAS, Lake, document_row

log = logging.getLogger(__name__)
_MAX_ATTEMPTS = 5


def _row_doc(r: dict) -> Document:
    return Document(
        id=r["id"], source=r["source"] or "", kind=r["kind"] or "article",
        origin_id=r["origin_id"] or "", url=r["url"] or "", title=r["title"] or "",
        snippet=r["snippet"] or "", language=r["language"] or "",
        published_at=r["published_at"], fetched_at=r["fetched_at"],
        attrs=json.loads(r["attrs"] or "{}"))


def _place(r: dict) -> tuple | None:
    return None if r["lat"] is None else (r["place_name"], r["lat"], r["lon"])


def _retriage_rows(rows: list[dict], triage: Triage, stats: dict[str, int]) -> list[dict]:
    schema = SCHEMAS["documents"]
    thr = triage.cfg.relevant
    out: list[dict] = []
    for old in rows:
        new = document_row(_row_doc(old), triage.assess(_row_doc(old)))
        # normalise through the schema (float32 etc.) so unchanged rows compare equal
        new = pa_roundtrip(new, schema)
        stats["docs"] += 1
        if new != old:
            stats["docs_changed"] += 1
        op, np_ = _place(old), _place(new)
        if op is not None and np_ is None:
            stats["became_unlocated"] += 1
        elif op is None and np_ is not None:
            stats["newly_located"] += 1
        elif op != np_:
            stats["place_changed"] += 1
        was, now = (old["relevance"] or 0.0) >= thr, (new["relevance"] or 0.0) >= thr
        if was and not now:
            stats["relevance_dropped_below"] += 1
        elif now and not was:
            stats["relevance_rose_above"] += 1
        out.append(new)
    return out


def pa_roundtrip(row: dict, schema: Any) -> dict:
    import pyarrow as pa

    from osint.store.lake import _table_rows

    return _table_rows(pa.Table.from_pylist([row], schema=schema))[0]


def retriage(lake: Lake, triage: Triage, *, since: datetime | None = None,
             dry_run: bool = False) -> dict[str, int]:
    """Re-triage every documents partition from ``since`` (None = all). Returns counts."""
    stats = dict.fromkeys(("partitions", "partitions_rewritten", "docs", "docs_changed",
                           "place_changed", "became_unlocated", "newly_located",
                           "relevance_dropped_below", "relevance_rose_above"), 0)
    for date, hour in lake.partitions("documents", since):
        for _ in range(_MAX_ATTEMPTS):
            rows, files = lake.read_partition("documents", date, hour)
            if not rows:
                break
            part = dict.fromkeys(stats, 0)
            new = _retriage_rows(rows, triage, part)
            if part["docs_changed"] == 0 and len(files) == 1:
                break  # nothing to change (a multi-file partition is left to compaction)
            if dry_run or part["docs_changed"] == 0:
                break
            if lake.rewrite_partition("documents", date, hour, new, files):
                part["partitions_rewritten"] = 1
                break
            log.info("partition %s/%02d changed under us; retrying", date, hour)
        else:
            raise RuntimeError(f"partition {date} hour {hour} kept changing; gave up")
        if not rows:
            continue
        stats["partitions"] += 1
        for k, v in part.items():
            if k != "partitions":
                stats[k] += v
        if dry_run:
            stats["partitions_rewritten"] = 0
    return stats


def _event_snapshot(store: Any) -> dict[str, tuple]:
    """doc_id -> (title, place_name) of the event that holds it (news events)."""
    with store._lock:
        rows = store._db.execute(
            "SELECT d.doc_id, e.title, e.place_name FROM events e JOIN event_documents d "
            "ON d.event_id = e.id WHERE e.id NOT IN (SELECT event_id FROM sensor_events)"
        ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def event_examples(before: dict[str, tuple], after: dict[str, tuple], n: int = 8) -> list[dict]:
    """Events whose place changed: matched by a shared document id."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for doc_id, (title, old) in before.items():
        new = after.get(doc_id, (None, None))[1] if doc_id in after else "(event gone)"
        if new != old and (title, old) not in seen:
            seen.add((title, old))
            out.append({"title": title, "old_place": old, "new_place": new})
            if len(out) >= n:
                break
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osint.pipeline.retriage", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=None, help="only partitions of the last N days")
    ap.add_argument("--rebuild-events", action="store_true",
                    help="rebuild events from the lake afterwards")
    ap.add_argument("--dry-run", action="store_true", help="count changes, write nothing")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    st = Settings()
    lake = Lake(st.lake_dir)
    triage = Triage.default(st.config_dir / "triage.yaml", st.data_dir / "gazetteer")
    log.info("loading gazetteer ...")
    triage.preload()
    since = utcnow() - timedelta(days=args.days) if args.days else None
    stats = retriage(lake, triage, since=since, dry_run=args.dry_run)
    print(msgspec.json.format(msgspec.json.encode({"retriage": stats}), indent=2).decode())
    if args.rebuild_events and not args.dry_run:
        from osint.pipeline.events import EventsConfig, load_cameo_map, rebuild_events
        from osint.store.sqlite import SqliteStore

        store = SqliteStore(st.sqlite_path)
        before = _event_snapshot(store)
        res = rebuild_events(store, lake, EventsConfig.from_file(st.config_dir / "events.yaml"),
                             cameo_category=load_cameo_map(st.config_dir / "triage.yaml"))
        examples = event_examples(before, _event_snapshot(store))
        print(msgspec.json.format(msgspec.json.encode(
            {"rebuild_events": res, "place_change_examples": examples}), indent=2).decode())
        print("NOTE: restart the server now; its EventBuilder keeps an in-memory index.")
        store.close()
    lake.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
