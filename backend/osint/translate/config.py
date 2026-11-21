"""config/translate.yaml loader."""

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml


@dataclass
class TranslateConfig:
    enabled: bool = True
    backend: str = "claude_cli"
    model: str = "haiku"
    cli_path: str | None = None
    timeout_s: float = 180.0
    concurrency: int = 1
    batch_size: int = 40
    max_chars: int = 300
    max_calls_per_hour: int = 60
    max_calls_per_day: int = 600
    share_extraction_limiter: bool = True
    interval_s: float = 120.0
    initial_delay_s: float = 45.0
    max_calls_per_pass: int = 6
    max_events: int = 5000
    skip_statuses: list[str] = field(default_factory=lambda: ["historical"])
    retry_after_min: float = 30.0
    max_attempts: int = 3

    @classmethod
    def from_dict(cls, data: dict) -> "TranslateConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    @classmethod
    def load(cls, path: Path) -> "TranslateConfig":
        if not path.is_file():
            return cls()
        return cls.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
