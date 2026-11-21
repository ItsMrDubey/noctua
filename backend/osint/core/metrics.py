"""Minimal in-process metrics with Prometheus text output."""

from collections import defaultdict

_Key = tuple[str, tuple[tuple[str, str], ...]]


def _key(name: str, labels: dict[str, object]) -> _Key:
    return name, tuple(sorted((k, str(v)) for k, v in labels.items()))


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[_Key, float] = defaultdict(float)
        self._gauges: dict[_Key, float] = {}

    def inc(self, name: str, value: float = 1.0, **labels: object) -> None:
        self._counters[_key(name, labels)] += value

    def set(self, name: str, value: float, **labels: object) -> None:
        self._gauges[_key(name, labels)] = float(value)

    def get(self, name: str, **labels: object) -> float:
        k = _key(name, labels)
        if k in self._gauges:
            return self._gauges[k]
        return self._counters.get(k, 0.0)

    def render_prometheus(self) -> str:
        lines = []
        for (name, labels), value in sorted({**self._counters, **self._gauges}.items()):
            label_str = ",".join(f'{k}="{v}"' for k, v in labels)
            lines.append(f"{name}{{{label_str}}} {float(value)}" if labels else f"{name} {float(value)}")
        return "\n".join(lines) + "\n"
