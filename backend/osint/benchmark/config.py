"""Benchmark settings (config/benchmark.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class BenchmarkConfig:
    windows: dict[str, float] = field(default_factory=lambda: {"24h": 24.0, "7d": 168.0})
    cache_ttl_s: float = 300.0
    snapshot_interval_s: float = 3600.0
    first_snapshot_delay_s: float = 300.0
    snapshot_retention_days: float = 90.0
    duckdb_memory_limit: str = "256MB"
    duckdb_threads: int = 2
    decision_join_margin_h: float = 48.0
    city_or_better: tuple[str, ...] = ("point", "city")
    latency_max_h: float = 72.0
    residual_duplicate_window_h: float = 12.0
    min_labels: int = 20
    sample_default_n: int = 50
    sample_max_n: int = 200
    rejected_sample_n: int = 20

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BenchmarkConfig:
        base = cls()
        return cls(
            windows={str(k): float(v) for k, v in (d.get("windows") or base.windows).items()},
            cache_ttl_s=float(d.get("cache_ttl_s", base.cache_ttl_s)),
            snapshot_interval_s=float(d.get("snapshot_interval_s", base.snapshot_interval_s)),
            first_snapshot_delay_s=float(d.get("first_snapshot_delay_s",
                                               base.first_snapshot_delay_s)),
            snapshot_retention_days=float(d.get("snapshot_retention_days",
                                                base.snapshot_retention_days)),
            duckdb_memory_limit=str(d.get("duckdb_memory_limit", base.duckdb_memory_limit)),
            duckdb_threads=int(d.get("duckdb_threads", base.duckdb_threads)),
            decision_join_margin_h=float(d.get("decision_join_margin_h",
                                               base.decision_join_margin_h)),
            city_or_better=tuple(str(p) for p in d.get("city_or_better", base.city_or_better)),
            latency_max_h=float(d.get("latency_max_h", base.latency_max_h)),
            residual_duplicate_window_h=float(d.get("residual_duplicate_window_h",
                                                    base.residual_duplicate_window_h)),
            min_labels=int(d.get("min_labels", base.min_labels)),
            sample_default_n=int(d.get("sample_default_n", base.sample_default_n)),
            sample_max_n=int(d.get("sample_max_n", base.sample_max_n)),
            rejected_sample_n=int(d.get("rejected_sample_n", base.rejected_sample_n)),
        )

    @classmethod
    def from_file(cls, path: Path) -> BenchmarkConfig:
        if not path.is_file():
            return cls()
        with path.open(encoding="utf-8") as f:
            return cls.from_dict(yaml.safe_load(f) or {})

    def window_hours(self, name: str) -> float:
        """Hours of a named window; KeyError (-> 422 in the API) when unknown."""
        return self.windows[name]
