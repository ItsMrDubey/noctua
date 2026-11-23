"""config/terror.yaml loader (dataclasses mirror the YAML; unknown keys are ignored)."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml


def _pick(cls, d):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class LLMConfig:
    enabled: bool = True
    max_calls_per_hour: int = 30
    max_calls_per_day: int = 300
    max_per_run: int = 15
    concurrency: int = 1
    retry_after_min: float = 120.0
    max_attempts: int = 2


@dataclass
class SourcesConfig:
    enabled: bool = True
    timeout_s: float = 60.0
    doj_pages_per_pass: int = 4
    doj_backfill_pages: int = 30


@dataclass
class DedupeConfig:
    max_hours: float = 48.0
    max_km: float = 50.0
    min_cosine: float = 0.60


@dataclass
class IndicatorConfig:
    ucdp_recent_months: int = 3
    ucdp_baseline_months: int = 12
    news_window_days: int = 30


@dataclass
class TerrorConfig:
    enabled: bool = True
    interval_s: float = 900.0
    initial_delay_s: float = 180.0
    window_days: float = 21.0
    llm: LLMConfig = field(default_factory=LLMConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    dedupe: DedupeConfig = field(default_factory=DedupeConfig)
    indicators: IndicatorConfig = field(default_factory=IndicatorConfig)

    @classmethod
    def from_dict(cls, d: dict) -> TerrorConfig:
        d = dict(d or {})
        sub = {"llm": _pick(LLMConfig, d.pop("llm", None)),
               "sources": _pick(SourcesConfig, d.pop("sources", None)),
               "dedupe": _pick(DedupeConfig, d.pop("dedupe", None)),
               "indicators": _pick(IndicatorConfig, d.pop("indicators", None))}
        base = _pick(cls, d)
        for k, v in sub.items():
            setattr(base, k, v)
        return base

    @classmethod
    def load(cls, path: Path) -> TerrorConfig:
        if not path.is_file():
            return cls()
        return cls.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def load_calendar(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return list((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("dates") or [])
