"""NASA FIRMS archive backfill via the area API with a start date.

``/api/area/csv/{MAP_KEY}/{SOURCE}/world/{days<=10}/{YYYY-MM-DD}``. Standard-processing
products (MODIS_SP, VIIRS_SNPP_SP) for older dates, NRT products for the last
``nrt_days`` (SP lags ~2-3 months). Needs FIRMS_MAP_KEY; without it the source is
skipped. FIRMS allows 5000 transactions per 10 minutes per key; a token bucket spends
``cost_per_day`` tokens per requested day. The key is redacted from every message.

World-wide history is large, so a history filter applies (config/backfill.yaml):
``min_confidence`` (default nominal: drops low-confidence detections; MODIS numeric
confidence >= 30 counts as nominal, >= 80 as high) and ``min_frp`` (default 5 MW).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from osint.backfill.common import (
    BackfillContext,
    FetchError,
    Summary,
    TokenBucket,
    Window,
    fixed_chunks,
    get_with_retry,
    redact,
)
from osint.collectors.firms import BASE_URL, REQUEST_TIMEOUT, parse_csv
from osint.core.geo import utcnow
from osint.core.models import Observation
from osint.store.lake import observation_row

log = logging.getLogger("osint.backfill.firms")

SP_PRODUCTS = ("MODIS_SP", "VIIRS_SNPP_SP")
NRT_PRODUCTS = ("MODIS_NRT", "VIIRS_SNPP_NRT")
CONFIDENCE_RANK = {"low": 0, "nominal": 1, "high": 2}


def confidence_rank(label: str) -> int:
    """Rank of a parse_row confidence label ("High (85%)", "Nominal", "Low (12%)")."""
    head = (label or "").split(" ", 1)[0].casefold()
    return CONFIDENCE_RANK.get(head, 0)


def keep(obs: Observation, min_confidence: str, min_frp: float | None) -> bool:
    if confidence_rank(str(obs.attrs.get("confidence") or "")) < \
            CONFIDENCE_RANK.get(min_confidence.casefold(), 0):
        return False
    return not (min_frp is not None and (obs.value is None or obs.value < min_frp))


def area_url(key: str, product: str, w: Window, area: str = "world",
             base_url: str = BASE_URL) -> str:
    days = max(1, round((w.end - w.start) / timedelta(days=1)))
    if days > 10:
        raise ValueError("FIRMS area requests cover at most 10 days")
    return f"{base_url}/{key}/{product}/{area}/{days}/{w.start:%Y-%m-%d}"


def plan(window: Window, *, nrt_days: int, chunk_days: int = 10,
         sp_products: tuple[str, ...] = SP_PRODUCTS,
         nrt_products: tuple[str, ...] = NRT_PRODUCTS) -> list[tuple[str, Window]]:
    """(product, chunk) requests: SP before the NRT horizon, NRT after; day-aligned."""
    start = window.start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = window.end.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    horizon = end - timedelta(days=nrt_days)
    out: list[tuple[str, Window]] = []
    if start < horizon:
        for c in fixed_chunks(Window(start, min(horizon, end)), timedelta(days=chunk_days)):
            out += [(p, c) for p in sp_products]
    if horizon < end:
        for c in fixed_chunks(Window(max(start, horizon), end), timedelta(days=chunk_days)):
            out += [(p, c) for p in nrt_products]
    return out


async def run(ctx: BackfillContext, window: Window) -> Summary:
    summary = Summary(source="firms")
    key = ctx.settings.firms_map_key
    if not key:
        summary.state = "skipped"
        summary.message = ("FIRMS_MAP_KEY is not set (.env or environment); get a free key "
                           "at https://firms.modaps.eosdis.nasa.gov/api/map_key/ and re-run")
        return summary
    cp = ctx.checkpoint
    min_conf = str(ctx.opt("min_confidence", "nominal"))
    min_frp_raw = ctx.opt("min_frp", 5.0)
    min_frp = None if min_frp_raw in ("", "none", False) else float(min_frp_raw)
    requests = plan(window, nrt_days=int(ctx.opt("nrt_days", 60)),
                    chunk_days=min(10, int(ctx.opt("chunk_days", 10))),
                    sp_products=tuple(ctx.opt("sp_products", SP_PRODUCTS)),
                    nrt_products=tuple(ctx.opt("nrt_products", NRT_PRODUCTS)))
    summary.units_total = len(requests)
    bucket = TokenBucket(float(ctx.opt("transactions_per_10min", 5000)), 600.0)
    cost_per_day = float(ctx.opt("cost_per_day", 1.0))
    area = str(ctx.opt("area", "world"))
    filtered = 0
    for product, chunk in requests:
        ukey = f"{product}|{chunk.start:%Y-%m-%d}|{chunk.end:%Y-%m-%d}|{min_conf}|{min_frp}"
        if cp.is_done(ukey):
            summary.units_skipped += 1
            continue
        days = (chunk.end - chunk.start) / timedelta(days=1)
        await bucket.acquire(min(bucket.capacity, cost_per_day * days))
        t0 = time.perf_counter()
        try:
            r = await get_with_retry(ctx.http, area_url(key, product, chunk, area),
                                     secrets=[key], timeout=REQUEST_TIMEOUT, allow_status=())
            assert r is not None
            text = r.text
            if not text.startswith("latitude"):
                raise FetchError(redact(text[:200].strip(), key))
            obs = await asyncio.to_thread(parse_csv, text, product)
        except Exception as e:  # noqa: BLE001 — message is redacted; next chunk continues
            msg = redact(f"{ukey.split('|', 1)[0]} {chunk.start:%Y-%m-%d}: "
                         f"{type(e).__name__}: {e}", key)
            summary.errors.append(msg)
            cp.error(msg)
            log.error("firms %s", msg)
            continue
        kept = [o for o in obs if keep(o, min_conf, min_frp)
                and chunk.start <= o.ts < chunk.end + timedelta(days=1)]
        filtered += len(obs) - len(kept)
        seen: set[tuple[str, str]] = set()
        rows = []
        for o in kept:
            if (o.id, o.version) not in seen:
                seen.add((o.id, o.version))
                rows.append(observation_row(o))
        await asyncio.to_thread(ctx.write, "observations", rows, summary)
        log.info("firms %s %s..%s: %d detections, %d kept (filter), %.1fs", product,
                 f"{chunk.start:%Y-%m-%d}", f"{chunk.end:%Y-%m-%d}", len(obs), len(rows),
                 time.perf_counter() - t0)
        cp.mark_done(ukey, rows=len(rows))
        summary.units_done += 1
    summary.extra.update({"filtered_out": filtered, "rate_wait_s": round(bucket.waited_s, 1),
                          "finished_at": utcnow().isoformat()})
    return summary
