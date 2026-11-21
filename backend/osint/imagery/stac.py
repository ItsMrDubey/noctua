"""Earth Search STAC search (plain JSON POST) and before/after scene selection."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

BUCKET_PCT = 10.0  # cloud cover is compared in buckets so a slightly cloudier but much
#                    closer scene can win


@dataclass
class Scene:
    id: str
    collection: str
    acquired_at: datetime
    cloud_pct: float
    epsg: int | None
    bbox: tuple[float, float, float, float]
    visual: str | None = None            # 8-bit RGB COG (Sentinel-2 TCI)
    bands: dict[str, str] = field(default_factory=dict)  # red/green/blue COGs (Landsat)
    thumbnail: str | None = None

    @property
    def readable(self) -> bool:
        """Has assets reachable over anonymous HTTP(S) (Landsat on Earth Search is s3://)."""
        if self.visual:
            return self.visual.startswith("http")
        return (all(self.bands.get(k, "").startswith("http") for k in ("red", "green", "blue"))
                if self.bands else False)


def parse_dt(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _href(assets: dict, key: str) -> str | None:
    a = assets.get(key)
    return a.get("href") if isinstance(a, dict) else None


def parse_scene(feature: dict) -> Scene | None:
    props = feature.get("properties") or {}
    try:
        assets = feature.get("assets") or {}
        scene = Scene(
            id=feature["id"], collection=feature["collection"],
            acquired_at=parse_dt(props["datetime"]),
            cloud_pct=float(props.get("eo:cloud_cover", 100.0)),
            epsg=props.get("proj:epsg"), bbox=tuple(feature.get("bbox") or (0, 0, 0, 0))[:4],
            visual=_href(assets, "visual"),
            bands={k: h for k in ("red", "green", "blue") if (h := _href(assets, k))},
            thumbnail=_href(assets, "thumbnail") or _href(assets, "rendered_preview"))
    except (KeyError, ValueError, TypeError):
        return None
    return scene


def search_scenes(client: httpx.Client, stac_url: str, collection: str, lat: float, lon: float,
                  start: datetime, end: datetime, max_cloud: float, limit: int = 200
                  ) -> list[Scene]:
    body = {
        "collections": [collection],
        "intersects": {"type": "Point", "coordinates": [lon, lat]},
        "datetime": f"{start.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}/"
                    f"{end.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}",
        "query": {"eo:cloud_cover": {"lte": max_cloud}},
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
        "limit": limit,
    }
    r = client.post(f"{stac_url.rstrip('/')}/search", json=body)
    r.raise_for_status()
    scenes = (parse_scene(f) for f in r.json().get("features", []))
    return [s for s in scenes if s is not None and s.cloud_pct <= max_cloud]


def _rank(s: Scene, t: datetime) -> tuple[float, float]:
    return (s.cloud_pct // BUCKET_PCT, abs((s.acquired_at - t).total_seconds()))


def pick_before(scenes: list[Scene], t: datetime, *, lookback_days: int = 60,
                max_cloud: float = 30.0) -> Scene | None:
    """Best readable scene acquired before ``t`` (within lookback): cloud bucket, then nearest."""
    lo = t - timedelta(days=lookback_days)
    c = [s for s in scenes if lo <= s.acquired_at < t and s.cloud_pct <= max_cloud and s.readable]
    return min(c, key=lambda s: _rank(s, t)) if c else None


def pick_after(scenes: list[Scene], t: datetime, now: datetime, *, max_cloud: float = 30.0
               ) -> Scene | None:
    """Best readable scene acquired at/after ``t`` (up to now): cloud bucket, then earliest."""
    c = [s for s in scenes if t <= s.acquired_at <= now and s.cloud_pct <= max_cloud
         and s.readable]
    return min(c, key=lambda s: _rank(s, t)) if c else None
