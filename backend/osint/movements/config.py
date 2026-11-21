"""config/movements.yaml -> MovementsConfig (every key optional; defaults below)."""

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml


@dataclass
class LegsConfig:
    gap_min: float = 45
    ground_alt_ft: float = 1500
    ground_speed_kts: float = 60
    airport_radius_km: float = 15
    low_alt_ft: float = 4000
    min_points: int = 3
    in_flight_min: float = 20
    max_path_points: int = 40


@dataclass
class CorridorsConfig:
    default_hours: int = 168
    arc_points: int = 32
    max_callsigns: int = 6


@dataclass
class SurgesConfig:
    h3_res: int = 2
    bucket_hours: int = 6
    baseline_days: int = 7
    z_min: float = 2.5
    min_count: int = 3
    std_floor: float = 1.0
    min_baseline_days: float = 2


@dataclass
class NavalConfig:
    hours: int = 24
    max_track_points: int = 60
    name_prefixes: list[str] = field(default_factory=lambda: [
        "USNS", "USS", "HMS", "HMAS", "HMCS", "INS", "FS", "ITS", "ESPS", "JS", "ROKS", "TCG",
        "PLAN", "RFS", "HNLMS", "KRI", "BNS", "PNS"])


@dataclass
class MovementsConfig:
    cache_s: float = 60
    legs: LegsConfig = field(default_factory=LegsConfig)
    corridors: CorridorsConfig = field(default_factory=CorridorsConfig)
    surges: SurgesConfig = field(default_factory=SurgesConfig)
    naval: NavalConfig = field(default_factory=NavalConfig)

    @classmethod
    def from_dict(cls, d: dict | None) -> "MovementsConfig":
        d = d or {}
        sub = {"legs": LegsConfig, "corridors": CorridorsConfig, "surges": SurgesConfig, "naval": NavalConfig}
        kw: dict = {}
        for f in fields(cls):
            if f.name not in d:
                continue
            if f.name in sub:
                known = {x.name for x in fields(sub[f.name])}
                kw[f.name] = sub[f.name](**{k: v for k, v in (d[f.name] or {}).items() if k in known})
            else:
                kw[f.name] = d[f.name]
        return cls(**kw)

    @classmethod
    def load(cls, path: Path) -> "MovementsConfig":
        if not path.is_file():
            return cls()
        with path.open(encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh))
