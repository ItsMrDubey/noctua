"""Live snapshots shared by the worker (writer) and the API process (reader).

One JSON file per signal under ``data_dir/signals/``, replaced atomically. Readers decode a
file again only when its mtime/size changed."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import msgspec

from osint.state import write_atomic

log = logging.getLogger(__name__)

_ENC = msgspec.json.Encoder()
_NAME_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")


def signals_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "signals"


def _path(data_dir: Path, name: str) -> Path:
    if not name or not set(name) <= _NAME_OK:
        raise ValueError(f"invalid signal cache name {name!r}")
    return signals_dir(data_dir) / f"{name}.json"


def write_signal(data_dir: Path, name: str, obj: Any) -> int:
    """Atomically replace ``signals/<name>.json``; returns the size in bytes."""
    data = _ENC.encode(obj)
    write_atomic(_path(data_dir, name), data)
    return len(data)


class SignalReader:
    """Cached reader: ``get(name)`` is the decoded file or None (missing / unreadable)."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[tuple[int, int], Any]] = {}

    def get(self, name: str) -> Any:
        path = _path(self.data_dir, name)
        try:
            st = path.stat()
        except FileNotFoundError:
            return None
        sig = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._cache.get(name)
            if hit is not None and hit[0] == sig:
                return hit[1]
        try:
            value = msgspec.json.decode(path.read_bytes())
        except (OSError, msgspec.DecodeError) as exc:  # replaced mid-read: keep the last one
            log.debug("signal cache %s unreadable: %s", name, exc)
            return hit[1] if hit is not None else None
        with self._lock:
            self._cache[name] = (sig, value)
        return value
