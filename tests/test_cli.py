import logging

from osint.cli import configure_logging


def test_configure_logging_silences_httpx_request_logs():
    configure_logging()
    for name in ("httpx", "httpcore"):
        lg = logging.getLogger(name)
        assert lg.level == logging.WARNING
        assert not lg.isEnabledFor(logging.INFO)  # URL with FIRMS key is logged at INFO


class _Child:
    def __init__(self, pid, code):
        self.pid, self.code = pid, code

    def wait(self):
        return self.code


def test_supervise_restarts_with_backoff_and_gives_up(tmp_path):
    from osint.cli import supervise

    codes = iter([-9, 3, 0])
    sleeps = []
    rc = supervise(["x"], tmp_path / "s.log", sleep=sleeps.append, spawn=lambda c: _Child(1, next(codes)))
    assert rc == 0 and sleeps == [2, 4]
    log = (tmp_path / "s.log").read_text()
    assert "killed by signal 9" in log and "exit code 3" in log and "restart 2/5 in 4s" in log
    always = supervise(["x"], tmp_path / "t.log", sleep=lambda s: None, spawn=lambda c: _Child(2, 1))
    assert always == 1 and "giving up" in (tmp_path / "t.log").read_text()


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
