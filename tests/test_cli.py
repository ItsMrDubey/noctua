import logging

from osint.cli import configure_logging


def test_configure_logging_silences_httpx_request_logs():
    configure_logging()
    for name in ("httpx", "httpcore"):
        lg = logging.getLogger(name)
        assert lg.level == logging.WARNING
        assert not lg.isEnabledFor(logging.INFO)  # URL with FIRMS key is logged at INFO


class _Child:
    """A fake Popen: ``poll()`` yields ``polls`` in turn (the last one repeats)."""

    def __init__(self, pid, *polls, wait_code=0, hang=False):
        self.pid, self.polls, self.wait_code, self.hang = pid, list(polls), wait_code, hang
        self.terminated = False

    def poll(self):
        return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]

    def wait(self, timeout=None):
        import subprocess

        if self.hang and timeout is not None and not self.terminated:
            raise subprocess.TimeoutExpired("x", timeout)
        return self.wait_code

    def terminate(self):
        self.terminated = True


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_supervise_restarts_with_backoff_and_gives_up(tmp_path):
    from osint.cli import supervise

    codes = iter([-9, 3, 0])
    clk, spawned = _Clock(), []

    def spawn(cmd):
        spawned.append(clk.t)
        return _Child(1, next(codes))

    rc = supervise(["x"], tmp_path / "s.log", sleep=clk.sleep, clock=clk, spawn=spawn)
    # exits seen at the next poll (+0.5 s); then 2 s and 4 s backoff
    assert rc == 0 and spawned == [0.0, 2.5, 7.0]
    log = (tmp_path / "s.log").read_text()
    assert "[run] child pid=1 exited: killed by signal 9" in log and "exit code 3" in log
    assert "[run] restart 2/5 in 4s" in log
    clk2 = _Clock()
    always = supervise(["x"], tmp_path / "t.log", sleep=clk2.sleep, clock=clk2,
                       spawn=lambda c: _Child(2, 1))
    assert always == 1 and "giving up" in (tmp_path / "t.log").read_text()


def test_supervise_many_restarts_children_independently(tmp_path):
    from osint.cli import supervise_many

    clk = _Clock()
    worker_codes = iter([1, 0])
    spawned = []

    def spawn(cmd):
        spawned.append((cmd[0], clk.t))
        if cmd[0] == "worker":
            return _Child(10, None, next(worker_codes))
        return _Child(20, *([None] * 20), 0)  # api runs ~10 s, then stops cleanly

    rc = supervise_many({"worker": ["worker"], "api": ["api"]}, tmp_path / "s.log",
                        sleep=clk.sleep, clock=clk, spawn=spawn)
    assert rc == 0
    assert [n for n, _ in spawned].count("api") == 1  # never restarted by the worker crash
    assert [n for n, _ in spawned].count("worker") == 2
    log = (tmp_path / "s.log").read_text()
    assert "[worker] child pid=10 exited: exit code 1" in log
    assert "[worker] restart 1/5 in 2s" in log
    assert "[api] child pid=20 exited: exit code 0" in log
    assert "[api] restart" not in log


def test_supervise_many_interrupt_stops_both(tmp_path):
    from osint.cli import supervise_many

    kids = {"worker": _Child(1, None, hang=True, wait_code=1), "api": _Child(2, None)}
    calls = []

    def sleep(s):
        calls.append(s)
        if len(calls) == 3:
            raise KeyboardInterrupt

    rc = supervise_many({"worker": ["worker"], "api": ["api"]}, tmp_path / "s.log",
                        sleep=sleep, spawn=lambda c: kids[c[0]])
    assert rc == 0
    assert kids["worker"].terminated and not kids["api"].terminated  # api stopped by itself
    log = (tmp_path / "s.log").read_text()
    assert "[worker] interrupted; child stopped (exit code 1)" in log
    assert "[api] interrupted; child stopped (exit code 0)" in log


def test_main_supervise_default_runs_worker_and_api(tmp_path, monkeypatch):
    from osint import cli

    seen = {}

    def fake_many(children, log_path, **kw):
        seen["children"], seen["log"] = children, log_path
        return 0

    def fake_single(cmd, log_path, **kw):
        seen["single"] = cmd
        return 0

    monkeypatch.setenv("OSINT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "supervise_many", fake_many)
    monkeypatch.setattr(cli, "supervise", fake_single)
    for argv in (["supervise"], ["supervise", "--single"]):
        try:
            cli.main(argv)
        except SystemExit as e:
            assert e.code == 0
    assert list(seen["children"]) == ["worker", "api"]
    assert "main(['worker'])" in seen["children"]["worker"][-1]
    assert "main(['run'])" in seen["single"][-1]
    assert seen["log"] == tmp_path / "supervisor.log"


def test_crash_handlers_write_timestamped_log(tmp_path):
    import faulthandler
    import sys
    import threading

    from osint import cli

    hooks = sys.excepthook, threading.excepthook
    try:
        p = cli.install_crash_handlers(tmp_path)
        assert p.name == "crash.log" and "osint start" in p.read_text()
        assert faulthandler.is_enabled()
    finally:
        sys.excepthook, threading.excepthook = hooks
        faulthandler.disable()
        cli._crash_file.close()
