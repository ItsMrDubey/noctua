"""``config/storylines.yaml``: job cadence, linking points and thresholds, sensor rules."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# a reaction to an attack is "political", the fire it caused "infrastructure" or "disaster"
DEFAULT_COMPATIBLE = (
    ("armed_conflict", "maritime_security", "air_activity", "infrastructure", "political",
     "cyber_outage"),
    ("disaster", "infrastructure", "political", "economic", "cyber_outage"),
    ("civil_unrest", "political", "armed_conflict", "infrastructure"),
    ("economic", "infrastructure", "maritime_security", "political"),
)


def _pick(cls, d: dict[str, Any] | None):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class LinkConfig:
    """Pair scoring (``link.score_pair``) and thread coherence (``assemble``)."""

    link_threshold: float = 3.75
    max_gap_h: float = 96.0
    core_window_h: float = 72.0
    max_span_days: float = 14.0
    coherence_min: float = 0.40
    cos_floor: float = 0.45
    cos_full: float = 0.80
    cos_points: float = 4.0
    same_place_points: float = 3.0
    near_points: float = 2.0
    near_km: float = 25.0
    mention_points: float = 2.0
    country_points: float = 0.5
    busy_place_events: int = 6
    busy_place_days: int = 3
    term_points_max: float = 3.0
    term_df_full: int = 3
    term_df_zero: float = 0.04
    name_term: float = 1.5
    generic_term: float = 0.35
    sensor_ref_points: float = 2.0
    gap_penalty: float = 3.0
    anchor_min: float = 1.0
    far_km: float = 300.0
    compatible_categories: list[list[str]] = field(default_factory=lambda: [
        list(g) for g in DEFAULT_COMPATIBLE])

    def compatible(self, a: str, b: str) -> bool:
        """Same category, or both in one ``compatible_categories`` group."""
        if a == b:
            return True
        return any(a in g and b in g for g in self.compatible_categories)


@dataclass
class SensorConfig:
    detectors: list[str] = field(default_factory=lambda: [
        "seismic", "hazard_wildfire", "hazard_flood", "hazard_tropical_cyclone", "outage",
        "gnss_jam"])
    max_span_h: float = 168.0
    near_km: float = 40.0
    before_h: float = 24.0
    after_h: float = 72.0


@dataclass
class StorylineConfig:
    enabled: bool = True
    interval_s: float = 300.0
    initial_delay_s: float = 150.0
    window_days: float = 21.0
    rebuild_h: float = 6.0
    min_members: int = 2
    linking: LinkConfig = field(default_factory=LinkConfig)
    sensor: SensorConfig = field(default_factory=SensorConfig)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> StorylineConfig:
        d = dict(d or {})
        link = _pick(LinkConfig, d.pop("linking", None))
        sensor = _pick(SensorConfig, d.pop("sensor", None))
        out = _pick(cls, d)
        out.linking, out.sensor = link, sensor
        return out

    @classmethod
    def load(cls, path: Path) -> StorylineConfig:
        if not path.is_file():
            log.warning("storylines config %s missing; using defaults", path)
            return cls()
        with path.open(encoding="utf-8") as f:
            return cls.from_dict(yaml.safe_load(f) or {})
