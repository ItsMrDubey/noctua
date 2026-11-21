"""Records produced by collectors and consumed by the pipeline."""

from datetime import datetime

import msgspec


class Position(msgspec.Struct, frozen=True, kw_only=True):
    track_id: str  # "air:<icao hex>" | "sea:<mmsi>"
    kind: str  # "air" | "sea"
    ts: datetime  # timezone-aware UTC
    lat: float
    lon: float
    source: str
    alt_ft: int | None = None
    speed_kts: float | None = None
    heading_deg: float | None = None


class TrackInfo(msgspec.Struct, kw_only=True):
    track_id: str
    kind: str
    ident: str
    name: str | None = None
    reg: str | None = None
    type_code: str | None = None
    operator: str | None = None
    category: str | None = None
    is_mil: bool = False
    high_priority: bool = False
    extra: dict[str, str] = msgspec.field(default_factory=dict)


class Observation(msgspec.Struct, frozen=True, kw_only=True):
    id: str  # stable id, e.g. "fire:<hash>" | "quake:<usgs id>"
    type: str  # "fire" | "quake"
    ts: datetime
    lat: float
    lon: float
    source: str
    value: float | None = None  # FRP for fires, magnitude for quakes
    version: str = ""  # changes when the upstream record is revised
    attrs: dict[str, object] = msgspec.field(default_factory=dict)


class Batch(msgspec.Struct, kw_only=True):
    source: str
    positions: list[Position] = msgspec.field(default_factory=list)
    tracks: list[TrackInfo] = msgspec.field(default_factory=list)
    observations: list[Observation] = msgspec.field(default_factory=list)

    def size(self) -> int:
        return len(self.positions) + len(self.tracks) + len(self.observations)
