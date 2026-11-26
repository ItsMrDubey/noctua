"""Records produced by collectors and consumed by the pipeline."""

import hashlib
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import msgspec

SNIPPET_MAX = 300  # spec: no full article text is stored
_TRACKING_PARAMS = {"fbclid", "gclid", "traffic_source", "ocid", "cmpid", "mc_cid", "mc_eid"}


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


def normalise_url(url: str) -> str:
    """Canonical form for dedupe: lower-case scheme/host, no fragment, no tracking params,
    sorted query, no trailing slash."""
    parts = urlsplit(url.strip())
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or ""
    netloc = parts.netloc.lower().removeprefix("www.")
    scheme = parts.scheme.lower()
    if scheme == "http":  # http and https copies of one article are the same document
        scheme = "https"
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def document_id(url: str, origin_id: str = "") -> str:
    """Return "doc:" + sha1(normalised url)[:16]. ``origin_id`` is only passed for sources whose
    records are finer-grained than an article (GDELT: many events per source URL)."""
    key = normalise_url(url) + (f"#{origin_id}" if origin_id else "")
    return "doc:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def clip_snippet(text: str | None, limit: int = SNIPPET_MAX) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


class Document(msgspec.Struct, frozen=True, kw_only=True):
    """A news article / feed item / coded GDELT event. Metadata + link only, never full text."""

    id: str  # document_id(url[, origin_id])
    source: str  # collector or "rss:<feed>"
    url: str
    title: str
    snippet: str  # <= SNIPPET_MAX characters
    language: str  # ISO 639-1, "" when unknown (triage detects it)
    published_at: datetime  # timezone-aware UTC
    fetched_at: datetime
    kind: str  # "article" | "gdelt_event"
    origin_id: str = ""  # upstream id: GDELT GlobalEventID, RSS guid, ...
    attrs: dict[str, object] = msgspec.field(default_factory=dict)


class Batch(msgspec.Struct, kw_only=True):
    source: str
    positions: list[Position] = msgspec.field(default_factory=list)
    tracks: list[TrackInfo] = msgspec.field(default_factory=list)
    observations: list[Observation] = msgspec.field(default_factory=list)
    documents: list[Document] = msgspec.field(default_factory=list)

    def size(self) -> int:
        return (len(self.positions) + len(self.tracks) + len(self.observations)
                + len(self.documents))


class PlaceHit(msgspec.Struct, frozen=True, kw_only=True):
    """A place found in a document (gazetteer match or coded geo)."""

    name: str
    country: str  # ISO 3166-1 alpha-2 ("" if unknown; GDELT FIPS codes are converted)
    lat: float
    lon: float
    precision: str  # "city" | "admin1" | "country"
    confidence: float  # 0..1
    matched: str = ""  # the text that matched
    admin1: str = ""
    geonameid: int | None = None
    feature: str = ""  # GeoNames feature code, "sea", "gdelt:<geo type>", ...


class TriageResult(msgspec.Struct, frozen=True, kw_only=True):
    language: str
    relevance: float  # 0..1
    categories: list[str] = msgspec.field(default_factory=list)  # most relevant first
    places: list[PlaceHit] = msgspec.field(default_factory=list)
    best_place: PlaceHit | None = None
    scores: dict[str, float] = msgspec.field(default_factory=dict)  # per-category
