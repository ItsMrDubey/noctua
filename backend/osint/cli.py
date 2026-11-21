"""Command line entry point: `osint run`, `osint backfill <source>`."""

import argparse
import logging

import uvicorn

from osint.api.app import create_app
from osint.api.security import is_loopback
from osint.config import Settings, load_sources
from osint.core.collector import build_collectors
from osint.runtime import Runtime


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO, which would leak API keys (e.g. FIRMS)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def check_bind(settings: Settings) -> None:
    """Refuse to expose the API beyond loopback without an API token."""
    if not is_loopback(settings.host) and not settings.api_token:
        raise SystemExit(
            f"refusing to bind {settings.host}: set OSINT_API_TOKEN (and OSINT_ALLOWED_HOSTS "
            "for the hostnames clients use) or bind to 127.0.0.1")


def add_backfill_parser(sub: argparse._SubParsersAction) -> None:
    from osint.backfill import SOURCES

    p = sub.add_parser("backfill", help="load historical data for one source (resumable)")
    p.add_argument("source", choices=SOURCES)
    p.add_argument("--days", type=int, help="the last N days (default per source)")
    p.add_argument("--since", help="start date YYYY-MM-DD (inclusive)")
    p.add_argument("--until", help="end date YYYY-MM-DD (inclusive; default now)")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch and parse, but write nothing (no lake, events or checkpoints)")
    p.add_argument("--no-cluster", action="store_true",
                   help="gdelt: skip event clustering of the backfilled documents")
    p.add_argument("--config", help="backfill config (default config/backfill.yaml)")


def run_backfill(args: argparse.Namespace) -> int:
    import asyncio
    from pathlib import Path

    from osint.backfill.common import DEFAULT_CONFIG, parse_date
    from osint.backfill.runner import Options, run

    if args.days is not None and (args.since or args.until):
        raise SystemExit("use either --days or --since/--until")
    opts = Options(source=args.source, days=args.days,
                   since=parse_date(args.since) if args.since else None,
                   until=parse_date(args.until) if args.until else None,
                   dry_run=args.dry_run, no_cluster=args.no_cluster,
                   config=Path(args.config) if args.config else DEFAULT_CONFIG)
    summary = asyncio.run(run(opts))
    return 1 if summary.state == "failed" else 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="osint")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run collectors and the API server")
    add_backfill_parser(sub)
    args = parser.parse_args(argv)

    if args.cmd == "backfill":
        configure_logging()
        raise SystemExit(run_backfill(args))

    if args.cmd == "run":
        configure_logging()
        settings = Settings()
        check_bind(settings)
        collectors = build_collectors(load_sources(settings.config_dir))
        app = create_app(Runtime(settings, collectors))
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
