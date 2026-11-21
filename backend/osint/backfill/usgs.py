"""USGS FDSN event-query backfill (earthquakes and other seismic events, worldwide).

Monthly chunks via https://earthquake.usgs.gov/fdsnws/event/1/query (GeoJSON). The API
returns at most 20,000 events per query (HTTP 400 "exceeds search limit" beyond that);
a chunk that hits the cap is split in half until it fits. Features are parsed with the
live collector's ``parse_feature`` and written to the lake ``observations`` dataset,
skipping (id, version) pairs already present. One checkpoint per chunk.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

import httpx

from osint.backfill.common import (
    BackfillContext,
    FetchError,
    Polite,
    Summary,
    Window,
    get_with_retry,
    month_chunks,
    split_half,
)
from osint.collectors.usgs import parse_feature
from osint.core.models import Observation
from osint.store.lake import observation_row

log = logging.getLogger("osint.backfill.usgs")

QUERY_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
MAX_PER_QUERY = 20_000
MIN_SPLIT = timedelta(hours=1)


class CapExceeded(Exception):
    pass


def query_params(w: Window, min_magnitude: float) -> dict[str, str]:
    return {"format": "geojson", "starttime": w.start.strftime("%Y-%m-%dT%H:%M:%S"),
            "endtime": w.end.strftime("%Y-%m-%dT%H:%M:%S"), "minmagnitude": str(min_magnitude),
            "orderby": "time-asc", "limit": str(MAX_PER_QUERY)}


async def fetch_chunk(http: httpx.AsyncClient, w: Window, *, min_magnitude: float,
                      polite: Polite, url: str = QUERY_URL,
                      max_per_query: int = MAX_PER_QUERY) -> list[dict]:
    """All features in ``w``, splitting the window while a query hits the cap."""
    async with polite:
        try:
            r = await get_with_retry(http, url, params=query_params(w, min_magnitude),
                                     timeout=180, allow_status=())
        except FetchError as e:
            if "limit" not in str(e).lower() or "HTTP 400" not in str(e):
                raise
            r = None  # 400 "exceeds search limit": split
    features = [] if r is None else (r.json().get("features") or [])
    if r is not None and len(features) < max_per_query:
        return features
    if w.end - w.start <= MIN_SPLIT:
        if r is None:
            raise CapExceeded(f"{w.start}..{w.end} exceeds the search limit")
        log.warning("usgs: %s..%s returned the cap (%d); keeping it", w.start, w.end,
                    len(features))
        return features
    a, b = split_half(w)
    log.info("usgs: %s..%s hit the %d cap; splitting", w.start, w.end, max_per_query)
    return (await fetch_chunk(http, a, min_magnitude=min_magnitude, polite=polite, url=url,
                              max_per_query=max_per_query)
            + await fetch_chunk(http, b, min_magnitude=min_magnitude, polite=polite, url=url,
                                max_per_query=max_per_query))


def parse_features(features: list[dict]) -> tuple[list[Observation], int]:
    out: list[Observation] = []
    bad = 0
    for f in features:
        try:
            obs = parse_feature(f)
        except (ValueError, TypeError, KeyError):
            bad += 1
            continue
        if obs is None:
            bad += 1
        else:
            out.append(obs)
    return out, bad


def existing_keys(ctx: BackfillContext, w: Window) -> set[tuple[str, str]]:
    rows = ctx.lake.query(
        "SELECT DISTINCT id, version FROM observations WHERE type = 'quake' "
        "AND ts >= ? AND ts < ?", [w.start, w.end], start=w.start, end=w.end)
    return {(r[0], r[1]) for r in rows}


async def run(ctx: BackfillContext, window: Window) -> Summary:
    summary = Summary(source="usgs")
    cp = ctx.checkpoint
    chunks = month_chunks(window)
    summary.units_total = len(chunks)
    polite = Polite(1, float(ctx.opt("min_interval_s", 1.0)))
    min_mag = float(ctx.opt("min_magnitude", 2.5))
    max_q = int(ctx.opt("max_per_query", MAX_PER_QUERY))
    url = str(ctx.opt("query_url", QUERY_URL))
    for chunk in chunks:
        key = f"{chunk.start:%Y-%m-%dT%H}..{chunk.end:%Y-%m-%dT%H}|m{min_mag}"
        if cp.is_done(key):
            summary.units_skipped += 1
            continue
        t0 = time.perf_counter()
        try:
            features = await fetch_chunk(ctx.http, chunk, min_magnitude=min_mag,
                                         polite=polite, url=url, max_per_query=max_q)
        except (FetchError, CapExceeded, ValueError) as e:
            msg = f"{key}: {e}"
            summary.errors.append(msg)
            cp.error(msg)
            log.error("usgs %s", msg)
            continue
        obs, bad = parse_features(features)
        have = existing_keys(ctx, chunk)
        rows = []
        for o in obs:
            if (o.id, o.version) in have:
                continue
            have.add((o.id, o.version))
            rows.append(observation_row(o))
        ctx.write("observations", rows, summary)
        log.info("usgs %s: %d features, %d malformed, %d new, %.1fs", key, len(features), bad,
                 len(rows), time.perf_counter() - t0)
        cp.mark_done(key, rows=len(rows), features=len(features))
        summary.units_done += 1
    return summary
