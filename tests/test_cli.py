import logging

from osint.cli import configure_logging


def test_configure_logging_silences_httpx_request_logs():
    configure_logging()
    for name in ("httpx", "httpcore"):
        lg = logging.getLogger(name)
        assert lg.level == logging.WARNING
        assert not lg.isEnabledFor(logging.INFO)  # URL with FIRMS key is logged at INFO
