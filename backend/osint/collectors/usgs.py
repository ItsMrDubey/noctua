"""USGS real-time earthquake (and explosion/quarry blast) feed, worldwide."""

import logging
from datetime import UTC, datetime, timedelta

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.geo import utcnow
from osint.core.models import Batch, Observation

log = logging.getLogger(__name__)

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

    def _window(self, cursor: dict | None, now: datetime) -> str:
        if not cursor:
            return self.cfg.get("backfill_window", "all_day")
        try:
            last = datetime.fromisoformat(cursor["last_poll"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
        except (KeyError, TypeError, ValueError):
            return "all_month"
        age = now - last
        if age < timedelta(minutes=50):
            return "all_hour"
        if age < timedelta(hours=23):
            return "all_day"
        if age < timedelta(days=6.5):
            return "all_week"
        return "all_month"

    async def poll(self, ctx: CollectorContext) -> Batch:
        started = utcnow()
        window = self._window(ctx.get_cursor(self.name), started)
        r = await ctx.http.get(FEED_URL.format(window=window), timeout=30)
        r.raise_for_status()
        features = r.json().get("features") or []
        batch = Batch(source=self.name)
        skipped = 0
        for f in features:
            try:
                obs = parse_feature(f)
            except (ValueError, TypeError, KeyError):
                skipped += 1
                continue
            if obs is not None:
                batch.observations.append(obs)
        if skipped:
            log.warning("usgs: skipped %d malformed feature(s) in %s", skipped, window)
        ctx.set_cursor(self.name, {"last_poll": started.isoformat(), "window": window})
        return batch
