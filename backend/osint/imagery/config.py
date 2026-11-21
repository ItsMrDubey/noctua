"""config/imagery.yaml loader."""

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml


@dataclass
class ImageryConfig:
    interval_s: float = 900.0
    max_per_run: int = 5
    concurrency: int = 3
    event_age_days: int = 7
    min_confidence: float = 0.0
    statuses: list[str] = field(
        default_factory=lambda: ["multi-source", "sensor-corroborated", "official"])
    precisions: list[str] = field(default_factory=lambda: ["city", "point"])
    stac_url: str = "https://earth-search.aws.element84.com/v1"
    collections: list[str] = field(default_factory=lambda: ["sentinel-2-l2a"])
    max_cloud_pct: float = 30.0
    lookback_days: int = 60
    window_km: float = 5.0
    retry_after_h: float = 24.0
    max_attempts: int = 3
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 45.0

    @classmethod
    def load(cls, path: Path) -> "ImageryConfig":
        if not path.is_file():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
