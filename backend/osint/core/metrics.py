"""Minimal in-process metrics with Prometheus text output."""

import threading
from collections import defaultdict

_Key = tuple[str, tuple[tuple[str, str], ...]]


def _key(name: str, labels: dict[str, object]) -> _Key:
    return name, tuple(sorted((k, str(v)) for k, v in labels.items()))


class Metrics:
    """Thread-safe: updated from the event loop and from ingest worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[_Key, float] = defaultdict(float)
        self._gauges: dict[_Key, float] = {}

    def inc(self, name: str, value: float = 1.0, **labels: object) -> None:
        k = _key(name, labels)
        with self._lock:
            self._counters[k] += value

    def set(self, name: str, value: float, **labels: object) -> None:
        k = _key(name, labels)
        with self._lock:
            self._gauges[k] = float(value)

    def get(self, name: str, **labels: object) -> float:
        k = _key(name, labels)
        with self._lock:
            if k in self._gauges:
                return self._gauges[k]
            return self._counters.get(k, 0.0)

    def render_prometheus(self) -> str:
        with self._lock:
            items = sorted({**self._counters, **self._gauges}.items())
        lines = []
        for (name, labels), value in items:
            label_str = ",".join(f'{k}="{v}"' for k, v in labels)
            lines.append(
                f"{name}{{{label_str}}} {float(value)}" if labels else f"{name} {float(value)}"
            )
        return "\n".join(lines) + "\n"
