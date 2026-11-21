"""One-off translation run outside the server: ``python -m osint.translate [--drain]``.

Scans all events (backfill), then translates the queue; ``--drain`` repeats until the queue
is empty or the caps are reached. Counts against the extraction caps like the live job.
"""

import argparse
import asyncio
import json
import logging
import time

from osint.config import Settings
from osint.llm import CallRateLimiter
from osint.pipeline.extract.config import ExtractionConfig
from osint.pipeline.extract.store import ExtractionStore
from osint.store.lake import Lake
from osint.store.sqlite import SqliteStore
from osint.translate.job import build_job


class _Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = SqliteStore(settings.sqlite_path)
        self.lake = Lake(settings.lake_dir)
        self.extraction_job = None
        cfg = ExtractionConfig.load(settings.config_dir / "extraction.yaml")
        ext = ExtractionStore(settings.data_dir / "extraction.sqlite")
        try:
            history = ext.call_times_since(time.time() - 86400)
        finally:
            ext.close()
        self.extraction_limiter = CallRateLimiter(cfg.max_calls_per_hour,
                                                  cfg.max_calls_per_day, history=history)


async def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m osint.translate")
    ap.add_argument("--drain", action="store_true", help="repeat until the queue is empty")
    ap.add_argument("--max-calls", type=int, default=None, help="CLI calls per pass")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    rt = _Runtime(Settings())
    job = build_job(rt)
    job.shared_limiter = rt.extraction_limiter
    try:
        while True:
            res = await job.run_once(args.max_calls)
            print(json.dumps(res), flush=True)
            if not args.drain or res["queue"] == 0 or res["capped"] or res["calls"] == 0:
                break
        print(json.dumps(job.status(), indent=1, default=str))
    finally:
        rt.store.close()
        rt.lake.close()
        job.store.close()


if __name__ == "__main__":
    asyncio.run(main())
