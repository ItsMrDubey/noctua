"""config/extraction.yaml loader."""

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml


@dataclass
class FetchConfig:
    timeout_s: float = 15.0
    max_bytes: int = 2_000_000
    max_chars: int = 12_000
    min_chars: int = 400
    alterlab_fallback: bool = True
    alterlab_max_credits: float = 5


@dataclass
class ExtractionConfig:
    enabled: bool = True
    backend: str = "claude_cli"
    model: str = "haiku"
    cli_path: str | None = None
    timeout_s: float = 90.0
    concurrency: int = 2
    max_calls_per_hour: int = 120
    max_calls_per_day: int = 1500
    interval_s: float = 120.0
    initial_delay_s: float = 60.0
    max_per_run: int = 20
    select_limit: int = 5000
    event_age_h: float = 168.0
    retry_after_min: float = 60.0
    max_attempts: int = 3
    min_confidence: float = 0.0
    min_sources: int = 1
    statuses: list[str] = field(default_factory=lambda: [
        "reported", "multi-source", "sensor-corroborated", "official"])
    mismatch_km: float = 300.0
    # results from an older SCHEMA_VERSION are re-extracted lazily, only for events updated
    # within reextract_age_h whose stored event_type is one of reextract_types (kinetic /
    # disaster: the ones impact sites apply to) and whose verdict is not unsupported; new
    # events always go first, and the hourly/daily caps still bound the total
    reextract_age_h: float = 48.0
    reextract_types: list[str] = field(default_factory=lambda: [
        "armed_conflict", "disaster", "infrastructure", "maritime_security", "air_activity"])
    fetch: FetchConfig = field(default_factory=FetchConfig)

    @classmethod
    def from_dict(cls, data: dict) -> "ExtractionConfig":
        data = dict(data or {})
        prio = data.pop("min_priority", None) or {}
        fetch = data.pop("fetch", None) or {}
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in data.items() if k in known}
        kw.update({k: v for k, v in prio.items() if k in ("min_confidence", "min_sources",
                                                           "statuses")})
        fknown = {f.name for f in fields(FetchConfig)}
        kw["fetch"] = FetchConfig(**{k: v for k, v in fetch.items() if k in fknown})
        return cls(**kw)

    @classmethod
    def load(cls, path: Path) -> "ExtractionConfig":
        if not path.is_file():
            return cls()
        return cls.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
