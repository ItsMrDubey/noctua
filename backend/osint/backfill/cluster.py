"""Event clustering over backfilled documents (after a document backfill).

The live ``EventBuilder`` follows a forward ``fetched_at`` cursor and only reads documents
published within ``index_window_h`` (72 h) of "now", so it never sees documents that
arrive late. This pass handles exactly those: documents whose ``fetched_at`` is more than
``index_window_h`` + 1 h after ``published_at`` (``late`` documents), so the two never
process the same document. It replays them hour by hour in published order through the
builder's public ``Clusterer`` (``EventBuilder.from_settings(...).clusterer``), with
"now" set to the end of each hour so time windows and GDELT staleness behave as they
did live, and persists each hour with ``SqliteStore.save_events``.

To avoid racing the live builder, only events whose ``t_end`` is older than the live
index window (minus a margin) are loaded or touched, and only hours before that horizon
are replayed. Progress (last finished hour) is the ``backfill:cluster`` checkpoint.

With the current clusterer GDELT rows never create events; they only attach to
article-backed events. A GDELT-only history therefore mostly yields rejections
(``no_article_backing``); those are counted in the summary but, unless
``persist_rejected`` is set, not written to ``gdelt_decisions`` (millions of rows that
carry no information for history). Accepted decisions are always written.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from osint.backfill.common import Summary, Window, fixed_chunks
from osint.core.geo import utcnow
from osint.pipeline.events import EventBuilder, PassStats, row_to_doc
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore

log = logging.getLogger("osint.backfill.cluster")

CURSOR = "backfill:cluster"
MARGIN = timedelta(hours=1)

# same columns (and order) as osint.pipeline.events._DOC_SQL, for row_to_doc
_LATE_DOC_SQL = """
SELECT id, source, kind, origin_id, url, title, snippet, language,
       epoch_us(published_at), epoch_us(fetched_at), attrs, relevance, categories, places,
       place_name, place_precision, place_confidence, lat, lon, h3_3, h3_5, h3_7
FROM documents
WHERE published_at >= ? AND published_at < ?
  AND (relevance >= ? OR kind = 'gdelt_event')
  AND epoch_us(fetched_at) - epoch_us(published_at) > ?
ORDER BY published_at, id
"""


def horizon(builder: EventBuilder, now: datetime) -> datetime:
    """Events/documents at or after this belong to the live builder."""
    return now - timedelta(hours=builder.cfg.index_window_h) - MARGIN


def late_lag_us(builder: EventBuilder) -> int:
    lag = timedelta(hours=builder.cfg.index_window_h) + MARGIN
    return int(lag / timedelta(microseconds=1))


def read_late_docs(lake: Lake, builder: EventBuilder, w: Window) -> list[Any]:
    rows = lake.query(_LATE_DOC_SQL, [w.start, w.end, builder.cfg.min_relevance,
                                      late_lag_us(builder)], start=w.start, end=w.end)
    out, seen = [], set()
    for r in rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        try:
            out.append(row_to_doc(r))
        except (TypeError, ValueError):
            log.warning("backfill cluster: skipped malformed document row %r", r[0])
    return out


_LATE_ARTICLE_SQL = """
SELECT count(*) FROM documents
WHERE published_at >= ? AND published_at < ? AND kind <> 'gdelt_event'
  AND relevance >= ? AND epoch_us(fetched_at) - epoch_us(published_at) > ?
"""


def has_late_articles(lake: Lake, builder: EventBuilder, w: Window) -> bool:
    """True if the window holds late (never live-clustered) non-GDELT documents. GDELT
    rows cannot create events, so a GDELT-only window has nothing to cluster."""
    rows = lake.query(_LATE_ARTICLE_SQL, [w.start, w.end, builder.cfg.min_relevance,
                                          late_lag_us(builder)], start=w.start, end=w.end)
    return bool(rows and rows[0][0])


def run_cluster(settings: Any, store: SqliteStore, lake: Lake, window: Window, *,
                dry_run: bool = False, now: datetime | None = None,
                builder: EventBuilder | None = None,
                persist_rejected: bool = False) -> Summary:
    """Replay late documents in ``window`` through the clusterer (sync; run in a thread)."""
    t0 = time.perf_counter()
    summary = Summary(source="cluster")
    now = now or utcnow()
    builder = builder or EventBuilder.from_settings(settings, store, lake)
    clusterer = builder.clusterer
    limit = horizon(builder, now)
    end = min(window.end, limit)
    if end <= window.start:
        summary.message = "window is inside the live builder's horizon; nothing to replay"
        return summary
    if not has_late_articles(lake, builder, Window(window.start, end)):
        summary.message = ("GDELT-only window (no late article documents): GDELT rows "
                           "cannot create events, cluster step skipped")
        return summary
    cursor = store.get_cursor(CURSOR) or {}
    done_until = cursor.get("until")
    start = window.start.replace(minute=0, second=0, microsecond=0)
    hours = fixed_chunks(Window(start, end), timedelta(hours=1))
    summary.units_total = len(hours)
    if done_until:
        done_dt = datetime.fromisoformat(done_until)
        if cursor.get("from") and datetime.fromisoformat(cursor["from"]) <= start:
            skip = [h for h in hours if h.end <= done_dt]
            summary.units_skipped = len(skip)
            hours = [h for h in hours if h.end > done_dt]
    if not hours:
        return summary
    # warm index: events that could match the first hour and are not live-owned
    since = hours[0].start - timedelta(hours=builder.cfg.index_window_h)
    old = [e for e in store.recent_events(since)
           if datetime.fromisoformat(e["t_end"]) < limit]
    clusterer.load(old)
    total = PassStats()
    for h in hours:
        docs = read_late_docs(lake, builder, h)
        if docs:
            hour_now = h.end
            res = clusterer.process(docs, hour_now)
            # never write events that the live builder may hold in memory
            changed = [clusterer.events[eid].to_row() for eid in res.changed
                       if eid in clusterer.events and clusterer.events[eid].t_end < limit]
            decisions = res.decisions if persist_rejected else [
                d for d in res.decisions if d.get("decision") == "accepted"]
            if not dry_run:
                store.save_events(changed, res.documents, res.fields, decisions)
            total.documents += len(docs)
            total.clustered += res.clustered
            total.created += len(res.created)
            total.updated += len(res.updated)
            total.gdelt_accepted += res.gdelt_accepted
            for k, v in res.gdelt_rejected.items():
                total.gdelt_rejected[k] = total.gdelt_rejected.get(k, 0) + v
            for k, v in res.skipped.items():
                total.skipped[k] = total.skipped.get(k, 0) + v
        if not dry_run:
            store.set_cursor(CURSOR, {
                "state": "running", "from": min(start, datetime.fromisoformat(
                    cursor.get("from") or start.isoformat())).isoformat(),
                "until": h.end.isoformat(), "updated_at": utcnow().isoformat(),
                "documents": total.documents, "created": total.created,
                "updated": total.updated})
        summary.units_done += 1
        if summary.units_done % 24 == 0:
            log.info("cluster: through %s - %d docs, %d events created, %d updated",
                     h.end.isoformat(), total.documents, total.created, total.updated)
    summary.rows = {"documents_read": total.documents, "events_created": total.created,
                    "events_updated": total.updated}
    summary.extra = {"gdelt_accepted": total.gdelt_accepted,
                     "gdelt_rejected": dict(total.gdelt_rejected),
                     "skipped": dict(total.skipped)}
    if not dry_run:
        c = store.get_cursor(CURSOR) or {}
        c.update({"state": "finished", "finished_at": utcnow().isoformat()})
        store.set_cursor(CURSOR, c)
    summary.seconds = time.perf_counter() - t0
    return summary
