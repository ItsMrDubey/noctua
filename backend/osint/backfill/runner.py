"""Entry point behind ``osint backfill``: window, checkpoint, source run, summary."""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from osint.backfill import SOURCES
from osint.backfill.common import (
    DEFAULT_CONFIG,
    BackfillContext,
    BackfillStore,
    Checkpoint,
    Summary,
    Window,
    load_config,
    new_http_client,
    resolve_window,
)
from osint.config import Settings
from osint.store.lake import Lake

log = logging.getLogger("osint.backfill")

DEFAULT_DAYS = {"gdelt": 30, "usgs": 365, "firms": 365, "ucdp": 0, "adsb_history": 7}
DEFAULT_SINCE = {"ucdp": date(1989, 1, 1)}


@dataclass
class Options:
    source: str
    days: int | None = None
    since: date | None = None
    until: date | None = None
    dry_run: bool = False
    no_cluster: bool = False
    config: Path = DEFAULT_CONFIG


def plan_window(opts: Options, cfg: dict) -> Window:
    default_since = cfg.get("default_since")
    if isinstance(default_since, str):
        default_since = date.fromisoformat(default_since)
    return resolve_window(
        days=opts.days, since=opts.since, until=opts.until,
        default_days=int(cfg.get("default_days") or DEFAULT_DAYS.get(opts.source) or 30),
        default_since=default_since or DEFAULT_SINCE.get(opts.source))


async def run(opts: Options, settings: Settings | None = None) -> Summary:
    if opts.source not in SOURCES:
        raise ValueError(f"unknown backfill source {opts.source!r} (one of {SOURCES})")
    settings = settings or Settings()
    for name in ("httpx", "httpcore"):  # request URLs would carry the FIRMS key
        logging.getLogger(name).setLevel(max(logging.WARNING,
                                             logging.getLogger(name).level))
    all_cfg = load_config(opts.config)
    cfg = all_cfg.get(opts.source, {})
    window = plan_window(opts, cfg)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = BackfillStore(settings.sqlite_path)
    lake = Lake(settings.lake_dir)
    cp = Checkpoint(store, opts.source, dry_run=opts.dry_run)
    module = importlib.import_module(f"osint.backfill.{opts.source}")
    t0 = time.perf_counter()
    log.info("backfill %s: window %s .. %s%s", opts.source, window.start.isoformat(),
             window.end.isoformat(), " (dry run)" if opts.dry_run else "")
    async with new_http_client() as http:
        ctx = BackfillContext(settings=settings, cfg=cfg, store=store, lake=lake, http=http,
                              checkpoint=cp, dry_run=opts.dry_run,
                              tmp_dir=settings.data_dir / "backfill" / "tmp",
                              no_cluster=opts.no_cluster)
        cp.start(window, dry_run=opts.dry_run)
        try:
            summary: Summary = await module.run(ctx, window)
        except BaseException as e:
            cp.error(f"{type(e).__name__}: {e}")
            cp.finish("failed", f"{type(e).__name__}: {e}"[:300])
            raise
        if summary.state == "finished" and summary.errors:
            summary.state = "finished_with_errors"
        if (opts.source == "gdelt" and not opts.dry_run and not opts.no_cluster
                and cfg.get("cluster", True)):
            from osint.backfill.cluster import run_cluster

            log.info("backfill gdelt: clustering late documents into events")
            cl = await asyncio.to_thread(
                run_cluster, settings, store, lake, window,
                persist_rejected=bool(cfg.get("persist_rejected", False)))
            summary.extra["cluster"] = {**cl.rows, **cl.extra,
                                        "hours": cl.units_done,
                                        "hours_already_done": cl.units_skipped,
                                        **({"note": cl.message} if cl.message else {})}
        summary.seconds = time.perf_counter() - t0
        cp.state["summary"] = {"rows": summary.rows, "seconds": round(summary.seconds, 1),
                               "units_done": summary.units_done,
                               "units_skipped": summary.units_skipped,
                               "errors": len(summary.errors)}
        cp.finish(summary.state, summary.message)
    for line in summary.lines():
        log.info(line)
    lake.close()
    store.close()
    return summary
