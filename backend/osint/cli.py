"""Command line entry point: `osint run`."""

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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="osint")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run collectors and the API server")
    args = parser.parse_args(argv)

    if args.cmd == "run":
        configure_logging()
        settings = Settings()
        check_bind(settings)
        collectors = build_collectors(load_sources(settings.config_dir))
        app = create_app(Runtime(settings, collectors))
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
