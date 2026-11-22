"""Command line entry point: `osint run` (single process), `osint worker` + `osint api`
(split; `osint supervise` runs both), `osint backfill <source>`."""

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


class _Supervised:
    """One child's restart state: exponential backoff, giving up after ``max_restarts``
    restarts within ``window_s`` seconds."""

    def __init__(self, name: str, cmd: list[str]) -> None:
        self.name, self.cmd = name, cmd
        self.proc = None
        self.started = 0.0
        self.restart_at = 0.0
        self.restarts: deque[float] = deque()
        self.attempt = 0
        self.done = False
        self.gave_up = False


def supervise_many(children: dict[str, list[str]], log_path: Path, *, max_restarts: int = 5,
                   window_s: float = 600, base_backoff_s: float = 2.0,
                   max_backoff_s: float = 60.0, poll_s: float = 0.5, stop_grace_s: float = 15.0,
                   sleep=time.sleep, clock=time.monotonic, spawn=subprocess.Popen) -> int:
    """Run each ``{name: cmd}`` as a child process, restarting each independently after a
    crash with exponential backoff; a child giving up (``max_restarts`` restarts within
    ``window_s``) or exiting 0 (clean stop) is not restarted. Returns once no child is left
    (1 if any gave up). Ctrl+C stops every child. Log lines are prefixed ``[name]``."""
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def note(name: str, msg: str) -> None:
        line = f"{_stamp()} [{name}] {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    kids = [_Supervised(name, cmd) for name, cmd in children.items()]

    def exited(k: _Supervised, code: int) -> None:
        note(k.name, f"child pid={k.proc.pid} exited: {describe_exit(code)} "
                     f"after {clock() - k.started:.0f}s")
        k.proc = None
        if code == 0:
            k.done = True
            return
        now = clock()
        while k.restarts and now - k.restarts[0] > window_s:
            k.restarts.popleft()
        if len(k.restarts) >= max_restarts:
            note(k.name, f"giving up: {max_restarts} restarts within {window_s:.0f}s")
            k.done = k.gave_up = True
            return
        k.restarts.append(now)
        k.attempt = k.attempt + 1 if now - k.started < window_s else 1
        delay = min(max_backoff_s, base_backoff_s * 2 ** (k.attempt - 1))
        note(k.name, f"restart {len(k.restarts)}/{max_restarts} in {delay:.0f}s")
        k.restart_at = now + delay

    try:
        while True:
            for k in kids:
                if k.done:
                    continue
                if k.proc is None:
                    if clock() >= k.restart_at:
                        k.started = clock()
                        k.proc = spawn(k.cmd)
                        note(k.name, f"started pid={k.proc.pid}: {' '.join(k.cmd)}")
                    continue
                code = k.proc.poll()
                if code is not None:
                    exited(k, code)
            if all(k.done for k in kids):
                return 1 if any(k.gave_up for k in kids) else 0
            sleep(poll_s)
    except KeyboardInterrupt:
        # a console Ctrl+C reaches the children too: give them time to shut down cleanly
        for k in kids:
            if k.proc is None:
                continue
            try:
                code = k.proc.wait(timeout=stop_grace_s)
            except subprocess.TimeoutExpired:
                k.proc.terminate()
                code = k.proc.wait()
            note(k.name, f"interrupted; child stopped ({describe_exit(code)})")
        return 0


def supervise(cmd: list[str], log_path: Path, **kwargs) -> int:
    """Supervise the single-process ``cmd`` (see supervise_many)."""
    return supervise_many({"run": cmd}, log_path, **kwargs)


def _child_cmd(sub: str) -> list[str]:
    return [sys.executable, "-c", f"from osint.cli import main; main([{sub!r}])"]


def run_worker(settings: Settings) -> None:
    """Collectors, ingest and every pipeline job, no HTTP server; publishes its state for
    `osint api` every ``settings.state_interval_s`` seconds."""
    import asyncio

    from osint.state import StatePublisher, state_path

    log = logging.getLogger("osint.worker")

    async def main() -> None:
        collectors = build_collectors(load_sources(settings.config_dir))
        runtime = Runtime(settings, collectors)
        publisher = StatePublisher(runtime, state_path(settings.data_dir),
                                   settings.state_interval_s)
        task = None
        try:
            await runtime.start()
            task = asyncio.create_task(publisher.run(), name="state_publisher")
            log.info("worker running; state -> %s", publisher.path)
            await asyncio.Event().wait()  # until Ctrl+C cancels us
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await runtime.stop()
            log.info("worker stopped")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


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
    sub.add_parser("run", help="run collectors, pipeline and the API server in one process")
    sub.add_parser("worker", help="run collectors and the pipeline (no HTTP); publishes state")
    sub.add_parser("api", help="serve the HTTP API from the stores and the worker's state")
    sp = sub.add_parser("supervise", help="run `osint worker` and `osint api` as child "
                        "processes; restart each with backoff after a crash")
    sp.add_argument("--single", action="store_true",
                    help="supervise the single-process `osint run` instead")
    add_backfill_parser(sub)
    args = parser.parse_args(argv)

    if args.cmd == "backfill":
        configure_logging()
        raise SystemExit(run_backfill(args))

    if args.cmd == "supervise":
        settings = Settings()
        log_path = settings.data_dir / "supervisor.log"
        if args.single:
            raise SystemExit(supervise(_child_cmd("run"), log_path))
        raise SystemExit(supervise_many(
            {"worker": _child_cmd("worker"), "api": _child_cmd("api")}, log_path))

    configure_logging()
    settings = Settings()
    install_crash_handlers(settings.data_dir)

    if args.cmd == "worker":
        run_worker(settings)
        return

    check_bind(settings)
    if args.cmd == "api":
        from osint.apiruntime import ApiRuntime

        app = create_app(ApiRuntime(settings))
    else:
        collectors = build_collectors(load_sources(settings.config_dir))
        app = create_app(Runtime(settings, collectors))
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
