"""Evidence points emitted by the indicators."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Evidence:
    kind: str                 # indicator kind (see fusion.positional_kinds, news, usni_prior)
    ts: datetime              # when the signal was observed (UTC)
    lat: float
    lon: float
    weight: float             # base weight before time decay
    description: str
    refs: list[dict] = field(default_factory=list)   # {type: track|doc|warning|cell|usni, id, url?}
    radius_km: float = 0.0    # spatial spread of the signal itself (warning area, news)
    polygon: list[list[float]] | None = None          # [[lon, lat], ...] when the signal is an area
    names: list[str] = field(default_factory=list)    # carrier names this evidence mentions
    half_life_h: float | None = None                  # override of the fusion half-life
    source_key: str = ""      # independence key (e.g. track id) - same key counts once per kind
    # every underlying source when one evidence aggregates several (an air-track fit built from
    # several flights): independence is judged on these, so the fit and the per-flight
    # indicators of the same aircraft are never counted as independent signals
    source_keys: list[str] = field(default_factory=list)
    detail: dict | None = None  # structured payload (e.g. the fitted track) for the UI

    def independence_keys(self, fallback: str) -> set[str]:
        return set(self.source_keys) or {self.source_key or fallback}

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "ts": self.ts.isoformat(), "lat": round(self.lat, 4),
             "lon": round(self.lon, 4), "weight": round(self.weight, 3),
             "description": self.description, "refs": self.refs,
             "radius_km": round(self.radius_km, 1)}
        if self.polygon:
            d["polygon"] = self.polygon
        if self.names:
            d["names"] = self.names
        if self.detail:
            d["detail"] = self.detail
        return d
