"""Command line entry point: `osint run`, `osint backfill <source>`."""

import argparse
import faulthandler
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

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


_crash_file = None


def _stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def install_crash_handlers(data_dir: Path) -> Path:
    """faulthandler (native crashes, fatal signals, hangs on SIGABRT) -> data/crash.log (append,
    timestamped per start), and unhandled exceptions (main thread and worker threads) logged."""
    global _crash_file
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "crash.log"
    _crash_file = path.open("a", buffering=1, encoding="utf-8")
    _crash_file.write(f"\n--- osint start {_stamp()} pid={os.getpid()} ---\n")
    faulthandler.enable(file=_crash_file, all_threads=True)
    log = logging.getLogger("osint.crash")
    prev = sys.excepthook

    def hook(etype, value, tb):
        if not issubclass(etype, KeyboardInterrupt):
            log.critical("unhandled exception", exc_info=(etype, value, tb))
            _crash_file.write(f"{_stamp()} unhandled {etype.__name__}: {value}\n")
        prev(etype, value, tb)

    def thread_hook(args):
        log.critical("unhandled exception in thread %s", args.thread and args.thread.name,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = hook
    threading.excepthook = thread_hook
    return path


def describe_exit(code: int) -> str:
    if code < 0:
        return f"killed by signal {-code}"
    if code > 0x7FFFFFFF - 1 or code >= 0xC0000000:  # Windows NTSTATUS crash codes
        return f"exit code {code} (0x{code:08X})"
    return f"exit code {code}"


def supervise(cmd: list[str], log_path: Path, *, max_restarts: int = 5, window_s: float = 600,
              base_backoff_s: float = 2.0, max_backoff_s: float = 60.0,
              sleep=time.sleep, clock=time.monotonic, spawn=subprocess.Popen) -> int:
    """Run ``cmd`` as a child; restart it after a crash with exponential backoff, giving up after
    ``max_restarts`` restarts within ``window_s`` seconds. Exit code 0 (clean stop) is not restarted."""
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def note(msg: str) -> None:
        line = f"{_stamp()} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    restarts: deque[float] = deque()
    attempt = 0
    while True:
        started = clock()
        child = spawn(cmd)
        note(f"started pid={child.pid}: {' '.join(cmd)}")
        try:
            code = child.wait()
        except KeyboardInterrupt:
            child.terminate()
            code = child.wait()
            note(f"interrupted; child stopped ({describe_exit(code)})")
            return 0
        note(f"child pid={child.pid} exited: {describe_exit(code)} after {clock() - started:.0f}s")
        if code == 0:
            return 0
        now = clock()
        while restarts and now - restarts[0] > window_s:
            restarts.popleft()
        if len(restarts) >= max_restarts:
            note(f"giving up: {max_restarts} restarts within {window_s:.0f}s")
            return 1
        restarts.append(now)
        attempt = attempt + 1 if now - started < window_s else 1
        delay = min(max_backoff_s, base_backoff_s * 2 ** (attempt - 1))
        note(f"restart {len(restarts)}/{max_restarts} in {delay:.0f}s")
        sleep(delay)


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
    sub.add_parser("supervise", help="run `osint run` as a child process; restart it with backoff after a crash")
    add_backfill_parser(sub)
    args = parser.parse_args(argv)

    if args.cmd == "backfill":
        configure_logging()
        raise SystemExit(run_backfill(args))

    if args.cmd == "supervise":
        settings = Settings()
        cmd = [sys.executable, "-c", "from osint.cli import main; main(['run'])"]
        raise SystemExit(supervise(cmd, settings.data_dir / "supervisor.log"))

    if args.cmd == "run":
        configure_logging()
        settings = Settings()
        install_crash_handlers(settings.data_dir)
        check_bind(settings)
        collectors = build_collectors(load_sources(settings.config_dir))
        app = create_app(Runtime(settings, collectors))
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
