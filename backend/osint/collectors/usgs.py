"""USGS real-time earthquake (and explosion/quarry blast) feed, worldwide."""

from datetime import UTC, datetime

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.geo import utcnow
from osint.core.models import Batch, Observation

FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{window}.geojson"


def parse_feature(f: dict) -> Observation | None:
    props = f.get("properties") or {}
    coords = (f.get("geometry") or {}).get("coordinates") or []
    if len(coords) < 2 or props.get("time") is None or not f.get("id"):
        return None
    return Observation(
        id=f"quake:{f['id']}",
        version=str(props.get("updated") or ""),
        type="quake",
        ts=datetime.fromtimestamp(props["time"] / 1000, UTC),
        lat=float(coords[1]),
        lon=float(coords[0]),
        source="usgs",
        value=props.get("mag"),
        attrs={
            "depth_km": coords[2] if len(coords) > 2 else None,
            "place": props.get("place"),
            "event_type": (props.get("type") or "earthquake").lower(),
            "mag_type": props.get("magType"),
            "felt": props.get("felt") or 0,
            "tsunami": bool(props.get("tsunami")),
            "url": props.get("url"),
        },
    )


@register
class UsgsCollector(PollCollector):
    name = "usgs"
    domain = "seismic"
    licence = "USGS public domain"
    commercial_ok = True
    default_interval_s = 120.0

    async def poll(self, ctx: CollectorContext) -> Batch:
        cursor = ctx.get_cursor(self.name)
        window = "all_hour" if cursor else self.cfg.get("backfill_window", "all_day")
        r = await ctx.http.get(FEED_URL.format(window=window), timeout=30)
        r.raise_for_status()
        features = r.json().get("features") or []
        batch = Batch(source=self.name)
        batch.observations.extend(o for o in map(parse_feature, features) if o is not None)
        ctx.set_cursor(self.name, {"last_poll": utcnow().isoformat(), "window": window})
        return batch
