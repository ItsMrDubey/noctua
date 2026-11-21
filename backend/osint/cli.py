"""Command line entry point: `osint run`."""

import argparse
import logging

import uvicorn

from osint.api.app import create_app
from osint.config import Settings, load_sources
from osint.core.collector import build_collectors
from osint.runtime import Runtime


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="osint")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run collectors and the API server")
    args = parser.parse_args(argv)

    if args.cmd == "run":
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        settings = Settings()
        collectors = build_collectors(load_sources(settings.config_dir))
        app = create_app(Runtime(settings, collectors))
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
