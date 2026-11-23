"""UCDP Candidate Events (monthly GED releases, <= 1 month lag) -> curated conflict events.

The UCDP API (ucdpapi.pcr.uu.se) now answers 401 without an ``x-ucdp-access-token``
header (checked 2026-11-23), but the same candidate data is published as public CSVs on
https://ucdp.uu.se/downloads/ (newest month plus a year-to-date file), so no token is
needed. Each poll reads the downloads page, and fetches every listed candidate CSV with a
conditional GET (ETag / Last-Modified kept per URL in the cursor): an unchanged file costs
one 304. Rows are mapped with the UCDP GED backfill's ``map_row`` and upserted through
``save_events`` (keyed ``ucdp-<id>``, the same ids the backfill writes, so both paths
converge on one row per event). Status/origin ``historical`` like the backfill.

Licence: CC BY 4.0, cite UCDP (see ``osint.api.licences.UCDP_CITE``).
"""

import asyncio
import csv
import io
import logging
from typing import Any

import httpx

from osint.backfill import ucdp
from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.geo import utcnow
from osint.core.models import Batch

log = logging.getLogger(__name__)

MAX_CSV_BYTES = 100 * 1024 * 1024  # a monthly file is ~2 MB, year-to-date ~20 MB


def parse_csv(data: bytes, dataset: str) -> tuple[list[dict], int]:
    """Mapped rows of one candidate CSV and the number of malformed rows (sync)."""
    now = utcnow()
    out: list[dict] = []
    bad = 0
    text = data.decode("utf-8-sig", errors="replace")
    for row in csv.DictReader(io.StringIO(text, newline="")):
        try:
            m = ucdp.map_row(row, dataset, now)
        except (ValueError, TypeError, KeyError):
            m = None
        if m is None:
            bad += 1
        else:
            out.append(m)
    return out, bad


def save(store: Any, mapped: list[dict]) -> None:
    for i in range(0, len(mapped), ucdp.BATCH):
        chunk = mapped[i:i + ucdp.BATCH]
        store.save_events([m["event"] for m in chunk], [m["document"] for m in chunk],
                          [f for m in chunk for f in m["fields"]])


@register
class UcdpCandidateCollector(PollCollector):
    name = "ucdp_candidate"
    domain = "events"
    licence = "CC BY 4.0 - UCDP Candidate Events Dataset, Uppsala Conflict Data Program"
    commercial_ok = True
    default_interval_s = 6 * 3600.0
    poll_timeout_s = 600.0

    async def _file(self, ctx: CollectorContext, url: str, state: dict[str, Any]
                    ) -> tuple[int, dict[str, Any]]:
        headers = {}
        if state.get("etag"):
            headers["If-None-Match"] = state["etag"]
        if state.get("last_modified"):
            headers["If-Modified-Since"] = state["last_modified"]
        r = await ctx.http.get(url, headers=headers, timeout=300)
        if r.status_code == 304:
            return 0, state
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        if len(r.content) > MAX_CSV_BYTES:
            raise RuntimeError(f"file too large ({len(r.content)} bytes)")
        dataset = ucdp.dataset_name(url)
        mapped, bad = await asyncio.to_thread(parse_csv, r.content, dataset)
        if bad:
            log.warning("ucdp_candidate %s: skipped %d malformed row(s)", dataset, bad)
        await asyncio.to_thread(save, ctx.store, mapped)
        log.info("ucdp_candidate %s: %d event(s) upserted", dataset, len(mapped))
        new_state = {k: v for k, v in (("etag", r.headers.get("etag", "")),
                                       ("last_modified", r.headers.get("last-modified", "")))
                     if v}
        new_state.update({"events": len(mapped), "imported_at": utcnow().isoformat()})
        return len(mapped), new_state

    async def poll(self, ctx: CollectorContext) -> Batch:
        batch = Batch(source=self.name)
        urls = list(self.cfg.get("urls") or [])
        if not urls:
            try:
                r = await ctx.http.get(self.cfg.get("downloads_url", ucdp.DOWNLOADS_URL),
                                       timeout=60)
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"ucdp_candidate: HTTP {e.response.status_code}") from None
            except httpx.HTTPError as e:
                raise RuntimeError(f"ucdp_candidate: {type(e).__name__}") from None
            urls = ucdp.candidate_urls(r.text)
            if not self.cfg.get("all_files", True):
                urls = urls[:1]  # the page lists the newest month first
        if not urls:
            raise RuntimeError("ucdp_candidate: no candidate CSV listed on the downloads page")
        cursor = ctx.get_cursor(self.name) or {}
        files: dict[str, dict] = dict(cursor.get("files") or {})
        errors: list[str] = []
        total = 0
        for url in reversed(urls):  # oldest first, so the newest release wins an upsert
            try:
                n, files[url] = await self._file(ctx, url, files.get(url, {}))
                total += n
            except (RuntimeError, httpx.HTTPError) as e:
                errors.append(f"{ucdp.dataset_name(url)}: {type(e).__name__}: {e}"[:200])
        for err in errors:
            log.warning("ucdp_candidate file failed: %s", err)
        if len(errors) == len(urls):
            raise RuntimeError("all UCDP candidate files failed: " + "; ".join(errors[:3]))
        ctx.set_cursor(self.name, {"files": {u: files[u] for u in urls if u in files},
                                   "last_poll": utcnow().isoformat(), "events": total,
                                   "failed": len(errors)})
        return batch
