"""config/fire_score.yaml loader: one dataclass per section, defaults mirror the YAML (a
missing file or key falls back to them; unknown keys are ignored)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

DEFAULT_WEIGHTS: dict[str, float] = {
    "base": 5, "context_cap": 75, "frontline": 55, "ucdp_hist": 25,
    "conflict_news_5km": 30, "conflict_news_city": 22, "conflict_news_25km": 12,
    "gate_min": 0.15, "gate_full": 40,
    "night": 8, "isolated": 8, "urban": 10, "infrastructure": 6, "high_frp": 6,
    "static_anomaly": 10, "large_cluster": -20, "burn_region": -15, "low_conf": -5,
    "static_site": -40, "gas_flare": -50, "volcano": -60,
}


@dataclass
class JobCfg:
    enabled: bool = True
    interval_s: float = 300.0
    initial_delay_s: float = 120.0
    window_h: float = 36.0
    rescore_h: float = 3.0
    retention_days: float = 8.0
    threshold: int = 50


@dataclass
class FrontlineCfg:
    enabled: bool = True
    url: str = "https://deepstatemap.live/api/history/last"
    refresh_h: float = 6.0
    probe_km: float = 1.0


@dataclass
class ReferenceCfg:
    refresh_days: float = 30.0
    volcano_url: str = (
        "https://webservices.volcano.si.edu/geoserver/GVP-VOTW/ows?service=WFS&version=2.0.0"
        "&request=GetFeature&typeName=GVP-VOTW:Smithsonian_VOTW_Holocene_Volcanoes"
        "&outputFormat=application%2Fjson&propertyName=Volcano_Number,Volcano_Name,"
        "Last_Eruption_Year,Country,GeoLocation")
    flare_url: str = ("https://eogdata.mines.edu/global_flare_data/"
                      "VIIRS_Global_flaring_d.7_slope_0.029353_2024_v20240730_web_IDmatch.xlsx")
    volcano_since_year: int = 1800


@dataclass
class HistoryCfg:
    days: int = 30
    cell_deg: float = 0.004
    refresh_h: float = 6.0
    min_history_days: int = 4
    min_days: int = 3
    min_fraction: float = 0.3
    anomaly_factor: float = 4.0
    anomaly_min_frp: float = 30.0


@dataclass
class ContextCfg:
    frontline_km: float = 40.0
    frontline_full_km: float = 3.0
    near_frontline_km: float = 15.0
    ucdp_km: float = 25.0
    ucdp_years: float = 5.0
    ucdp_saturation: int = 20
    ucdp_min_events: int = 3
    news_hours: float = 72.0
    news_km: float = 5.0
    news_city_km: float = 12.0
    news_wide_km: float = 25.0
    news_categories: list[str] = field(default_factory=lambda: [
        "armed_conflict", "infrastructure", "civil_unrest"])
    news_precisions: list[str] = field(default_factory=lambda: ["point", "city"])


@dataclass
class PatternCfg:
    isolated_km: float = 2.0
    isolated_max: int = 2
    cluster_km: float = 3.0
    cluster_min: int = 15
    region_km: float = 25.0
    region_min: int = 150
    urban_min_pop: int = 15000
    poi_km: float = 3.0
    high_frp: float = 50.0
    volcano_km: float = 5.0
    flare_km: float = 1.0


def _section(cls, data: Any):
    data = data if isinstance(data, dict) else {}
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class FireScoreConfig:
    job: JobCfg = field(default_factory=JobCfg)
    frontline: FrontlineCfg = field(default_factory=FrontlineCfg)
    reference: ReferenceCfg = field(default_factory=ReferenceCfg)
    history: HistoryCfg = field(default_factory=HistoryCfg)
    context: ContextCfg = field(default_factory=ContextCfg)
    pattern: PatternCfg = field(default_factory=PatternCfg)
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    @classmethod
    def from_dict(cls, data: dict | None) -> FireScoreConfig:
        data = data or {}
        weights = dict(DEFAULT_WEIGHTS)
        weights.update({k: float(v) for k, v in (data.get("weights") or {}).items()
                        if isinstance(v, int | float)})
        return cls(job=_section(JobCfg, data.get("job")),
                   frontline=_section(FrontlineCfg, data.get("frontline")),
                   reference=_section(ReferenceCfg, data.get("reference")),
                   history=_section(HistoryCfg, data.get("history")),
                   context=_section(ContextCfg, data.get("context")),
                   pattern=_section(PatternCfg, data.get("pattern")), weights=weights)

    @classmethod
    def load(cls, path: Path) -> FireScoreConfig:
        data = {}
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_dict(data)

    def w(self, name: str) -> float:
        return float(self.weights.get(name, 0.0))

    def fingerprint(self) -> str:
        """Changes whenever a scoring input of the config changes (stored per score: a
        config edit rescores the window on the next pass)."""
        d = asdict(self)
        for k in ("job", "frontline", "reference"):  # cadence / sources, not scoring
            d.pop(k)

        def norm(v):  # YAML 2 and default 2.0 are the same setting
            if isinstance(v, dict):
                return {k: norm(x) for k, x in v.items()}
            if isinstance(v, list):
                return [norm(x) for x in v]
            return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else v

        return hashlib.sha1(json.dumps(norm(d), sort_keys=True, default=str).encode()
                            ).hexdigest()[:12]
