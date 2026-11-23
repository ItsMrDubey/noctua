"""Consolidate already-split news events (cross-language fragments) - one-off command.

The live ``EventBuilder`` consolidates on its own (at warm start over its 72 h index, then
the events each pass touches; ``EventBuilder.consolidate``). This command does the same
over a chosen window from a separate process, e.g. right after upgrading, without waiting
for a worker restart::

    python -m osint.pipeline.merge --dry-run          # report what would merge
    python -m osint.pipeline.merge                    # merge (last 72 h)
    python -m osint.pipeline.merge --hours 168

It holds the event rebuild lock (``data/.event_rebuild.lock``): a live builder skips its
passes meanwhile and reloads its index afterwards (the ``event_rebuild`` marker's ``at``
is bumped). Merging is idempotent: merged-away events are gone, so a second run finds
nothing. ``rekey_external`` moves the links other stores hold (extraction verdicts,
imagery) to the surviving id; ``event_redirects`` resolves old ids for the API.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def rekey_external(data_dir: Path | None, merges: list[dict[str, Any]]) -> dict[str, int]:
    """Move loser-keyed rows of the side stores to the survivor (in merge order), only
    when the survivor has none: ``extraction.sqlite`` (supported verdicts only: a rejected
    article must not cap the merged event) and ``imagery.sqlite`` (per before/after role).
    Missing files are skipped; failures are logged, never raised (best effort: the
    redirect table still resolves the old id)."""
    out = {"extraction": 0, "imagery": 0}
    if data_dir is None or not merges:
        return out
    jobs = (("extraction", "extraction.sqlite",
             ("UPDATE OR IGNORE extractions SET event_id=? WHERE event_id=? "
              "AND verdict='supported'")),
            ("imagery", "imagery.sqlite",
             "UPDATE OR IGNORE imagery_assets SET event_id=? WHERE event_id=?"))
    for name, fname, sql in jobs:
        path = Path(data_dir) / fname
        if not path.is_file():
            continue
        try:
            db = sqlite3.connect(path, timeout=30.0)
            try:
                with db:
                    for m in merges:
                        out[name] += db.execute(sql, (m["survivor"], m["loser"])).rowcount
            finally:
                db.close()
        except sqlite3.Error:
            log.exception("moving %s links of merged events failed", name)
    return out


def main(argv: list[str] | None = None) -> int:
    from osint.config import Settings
    from osint.core.geo import utcnow
    from osint.pipeline.embed import build_embedder
    from osint.pipeline.events import (
        REBUILD_STATE,
        EventBuilder,
        EventsConfig,
        load_cameo_map,
        rebuild_lock_path,
    )
    from osint.store.lake import FileLock, Lake
    from osint.store.sqlite import SqliteStore

    ap = argparse.ArgumentParser(prog="osint.pipeline.merge", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=None,
                    help="events with t_end in the last N hours (default: index window)")
    ap.add_argument("--dry-run", action="store_true", help="report merges, write nothing")
    ap.add_argument("--lock-timeout", type=float, default=600.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:  # titles in any script on a Windows console
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    st = Settings()
    cfg = EventsConfig.from_file(Path(st.config_dir) / "events.yaml")
    embedder = build_embedder(cfg.embedding, Path(st.data_dir))
    if embedder is None:
        print("embedding disabled in config/events.yaml; nothing to do")
        return 1
    store = SqliteStore(st.sqlite_path, migrate=False)
    lake = Lake(st.lake_dir)
    try:
        with FileLock(rebuild_lock_path(store.path), args.lock_timeout):
            now = utcnow()
            builder = EventBuilder(store, lake, None, cfg, embedder=embedder,
                                   cameo_category=load_cameo_map(
                                       Path(st.config_dir) / "triage.yaml"))
            hours = args.hours if args.hours is not None else cfg.index_window_h
            n = builder.clusterer.load(store.recent_events(now - timedelta(hours=hours)))
            hydrated = builder.hydrate(save=not args.dry_run)
            merges = builder.clusterer.consolidate(None, now)
            report = merge_report(builder, merges)
            applied = 0
            if not args.dry_run and merges:
                applied = builder.persist_merges(merges)
                marker = store.get_cursor(REBUILD_STATE) or {}
                # a live builder reloads its index when the marker's "at" changes
                store.set_cursor(REBUILD_STATE, {**marker, "at": now.isoformat(),
                                                 "migrated": marker.get("migrated", True)})
            print(json.dumps({"events_loaded": n, "vectors_backfilled": hydrated,
                              "merges": len(merges), "applied": applied,
                              "dry_run": args.dry_run, "groups": report},
                             ensure_ascii=False, indent=2))
    finally:
        lake.close()
        store.close()
    return 0


def merge_report(builder: Any, merges: list[Any]) -> list[dict[str, Any]]:
    """Final survivor -> merged-away events (id, similarity, title), largest first."""
    final: dict[str, str] = {}
    for m in merges:
        final[m.loser] = m.survivor
    def root(e: str) -> str:
        while e in final:
            e = final[e]
        return e
    groups: dict[str, list[dict[str, Any]]] = {}
    for m in merges:
        title = next((f["quote"] for f in m.fields if f["field"] == "merged_event"), "")
        groups.setdefault(root(m.survivor), []).append(
            {"id": m.loser, "similarity": m.similarity, "title": title.split(": ", 1)[-1]})
    out = []
    for sid, losers in groups.items():
        ev = builder.clusterer.events.get(sid)
        out.append({"survivor": sid, "title": ev.title if ev else "",
                    "place": ev.place_name if ev else None,
                    "mentions": ev.mention_count if ev else None,
                    "languages": sorted(ev.languages) if ev else [], "merged": losers})
    out.sort(key=lambda g: -(g["mentions"] or 0))
    return out


if __name__ == "__main__":
    sys.exit(main())
