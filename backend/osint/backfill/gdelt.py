"""GDELT 2.0 event-export backfill: 15-minute ``*.export.CSV.zip`` files for the window.

URLs are constructed directly (``YYYYMMDDHHMMSS.export.CSV.zip`` at 15-minute steps)
rather than scanning the ~150 MB masterfilelist; a missing file (404) is counted, not an
error. Rows are parsed with the live collector's ``parse_export``, triaged (GDELT rows
carry coded geo, so no gazetteer is needed) and written to the lake ``documents``
dataset, skipping ids already in the lake for that day. One checkpoint per UTC day.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from osint.backfill.common import (
    BackfillContext,
    FetchError,
    Polite,
    Summary,
    Window,
    day_units,
    get_with_retry,
)
from osint.collectors.gdelt import MAX_ZIP_BYTES, parse_export
from osint.core.geo import utcnow
from osint.core.models import Document
from osint.pipeline.triage import Triage
from osint.store.lake import document_row

log = logging.getLogger("osint.backfill.gdelt")

BASE_URL = "https://data.gdeltproject.org/gdeltv2"
STEP = timedelta(minutes=15)
PUBLISH_LAG = timedelta(minutes=20)  # an export appears a few minutes after its stamp


def export_times(w: Window, now: datetime | None = None) -> list[datetime]:
    """Export timestamps (15-minute marks) inside [start, end) that should exist by now."""
    now = now or utcnow()
    s = w.start
    floor = s.replace(minute=s.minute - s.minute % 15, second=0, microsecond=0)
    first = floor if floor == s else floor + STEP
    out = []
    t = first
    while t < w.end and t <= now - PUBLISH_LAG:
        out.append(t)
        t += STEP
    return out


def export_url(ts: datetime, base_url: str = BASE_URL) -> str:
    return f"{base_url.rstrip('/')}/{ts.strftime('%Y%m%d%H%M%S')}.export.CSV.zip"


def existing_ids(ctx: BackfillContext, w: Window) -> set[str]:
    rows = ctx.lake.query(
        "SELECT DISTINCT id FROM documents WHERE source = 'gdelt' "
        "AND published_at >= ? AND published_at < ?",
        [w.start, w.end], start=w.start, end=w.end)
    return {r[0] for r in rows}


def triage_rows(triage: Triage, docs: list[Document], skip: set[str]) -> list[dict]:
    rows: list[dict] = []
    for d in docs:
        if d.id in skip:
            continue
        skip.add(d.id)
        rows.append(document_row(d, triage.assess(d)))
    return rows


async def _fetch_day(ctx: BackfillContext, polite: Polite, times: list[datetime],
                     summary: Summary) -> tuple[list[Document], int, list[str]]:
    base = ctx.opt("base_url", BASE_URL)
    missing = 0
    errors: list[str] = []

    async def one(ts: datetime) -> list[Document]:
        nonlocal missing
        url = export_url(ts, base)
        async with polite:
            try:
                r = await get_with_retry(ctx.http, url, timeout=120)
            except FetchError as e:
                errors.append(f"{url.rsplit('/', 1)[-1]}: {e}")
                return []
        if r is None:
            missing += 1
            return []
        data = r.content
        if len(data) > MAX_ZIP_BYTES:
            errors.append(f"{url.rsplit('/', 1)[-1]}: too large ({len(data)} bytes)")
            return []
        try:
            return await asyncio.to_thread(parse_export, data, utcnow())
        except Exception as e:  # noqa: BLE001 — one corrupt file must not stop the day
            errors.append(f"{url.rsplit('/', 1)[-1]}: {type(e).__name__}: {e}")
            return []

    results = await asyncio.gather(*(one(t) for t in times))
    docs = [d for chunk in results for d in chunk]
    return docs, missing, errors


async def run(ctx: BackfillContext, window: Window) -> Summary:
    summary = Summary(source="gdelt")
    cp = ctx.checkpoint
    days = day_units(window)
    summary.units_total = len(days)
    polite = Polite(int(ctx.opt("concurrency", 4)), float(ctx.opt("min_interval_s", 0.25)))
    triage = Triage.from_file(ctx.settings.config_dir / "triage.yaml")  # coded geo only
    max_errors = float(ctx.opt("max_missing_fraction", 0.5))
    files_total = missing_total = 0
    for day in days:
        key = day.start.strftime("%Y-%m-%d")
        times = export_times(day)
        midnight = day.start.replace(hour=0, minute=0, second=0, microsecond=0)
        partial = day.start != midnight or day.end != midnight + timedelta(days=1)
        ckey = key if not partial else f"{key}@{day.start:%H%M}-{day.end:%H%M}"
        if cp.is_done(key) or cp.is_done(ckey):
            summary.units_skipped += 1
            continue
        if not times:
            continue
        t0 = time.perf_counter()
        docs, missing, errors = await _fetch_day(ctx, polite, times, summary)
        files_total += len(times)
        missing_total += missing
        skip = await asyncio.to_thread(existing_ids, ctx, day)
        before = len(skip)
        rows = await asyncio.to_thread(triage_rows, triage, docs, skip)
        dupes = len(docs) - len(rows)
        await asyncio.to_thread(ctx.write, "documents", rows, summary)
        for e in errors:
            summary.errors.append(f"{key} {e}")
            cp.error(f"{key} {e}")
        log.info("gdelt %s: %d files (%d missing, %d failed), %d rows parsed, %d new, "
                 "%d already in lake (%d indexed), %.1fs", key, len(times), missing,
                 len(errors), len(docs), len(rows), dupes, before, time.perf_counter() - t0)
        if errors and len(errors) > max_errors * len(times):
            log.warning("gdelt %s: too many failures; day not checkpointed", key)
            continue
        if not errors:  # retried on the next run otherwise (re-run skips existing ids)
            cp.mark_done(ckey if partial else key, rows=len(rows), files=len(times),
                         missing=missing)
        summary.units_done += 1
    summary.extra.update({"files": files_total, "files_missing": missing_total})
    return summary
