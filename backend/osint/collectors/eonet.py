"""NASA EONET v3 (Earth Observatory Natural Event Tracker): open natural events, worldwide.

Endpoint (verified 2026-11-23, no key): https://eonet.gsfc.nasa.gov/api/v3/events
?status=open&days=N -> {"title", "events": [{id, title, description, link, closed,
categories[{id, title}], sources[{id, url}], geometry[{date, type: Point|Polygon,
coordinates, magnitudeValue, magnitudeUnit}]}]}. Without ``days`` the open list is ~7000
events / 5 MB (icebergs that never close), so the window is bounded.

One ``hazard`` observation per event at its latest geometry (polygons: vertex centroid, the
ring kept in attrs when small); version = latest geometry date + closed, so a moving storm
or a re-measured fire is stored again.
"""

import logging
import re

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.geo import utcnow
from osint.core.hazards import iso, observed_ts, parse_time, ring_centroid, thin_ring
from osint.core.models import Batch, Observation

log = logging.getLogger(__name__)

API_URL = "https://eonet.gsfc.nasa.gov/api/v3/events"
CATEGORIES = {"drought": "drought", "dustHaze": "dust_haze", "earthquakes": "earthquake",
              "floods": "flood", "landslides": "landslide", "manmade": "manmade",
              "seaLakeIce": "sea_lake_ice", "severeStorms": "tropical_cyclone",
              "snow": "snow", "tempExtremes": "extreme_temperature",
              "volcanoes": "volcano", "waterColor": "water_color", "wildfires": "wildfire"}
DEFAULT_EXCLUDE = ("seaLakeIce", "waterColor")  # icebergs / algal blooms: not disasters
# IRWIN lists planned burns as wildfires ("Prescribed Fire ... RX"): ~2/3 of US entries
DEFAULT_EXCLUDE_TITLE = r"(?i)^prescribed fire|\bRX\b"
MAX_RING_POINTS = 200


def _point(g: dict) -> tuple[float, float, list | None] | None:
    """(lon, lat, polygon ring or None) of one EONET geometry."""
    c = g.get("coordinates")
    if g.get("type") == "Point" and isinstance(c, list) and len(c) >= 2:
        return float(c[0]), float(c[1]), None
    if g.get("type") == "Polygon" and isinstance(c, list) and c and isinstance(c[0], list):
        centre = ring_centroid(c[0])
        if centre is not None:
            ring = thin_ring(c[0], MAX_RING_POINTS) if len(c[0]) > 2 else None
            return centre[0], centre[1], ring
    return None


def parse_event(e: dict, now, exclude=DEFAULT_EXCLUDE,
                exclude_title: re.Pattern | None = None) -> Observation | None:
    """One EONET event -> Observation (None: excluded category / title, or no usable
    geometry).
    Raises KeyError/ValueError/TypeError when malformed."""
    cats = [c.get("id") for c in e.get("categories") or [] if isinstance(c, dict)]
    if not cats or cats[0] in exclude:
        return None
    if exclude_title is not None and exclude_title.search(str(e.get("title") or "")):
        return None
    geoms = [g for g in e.get("geometry") or [] if isinstance(g, dict)]
    dated = sorted(((parse_time(g.get("date")), g) for g in geoms),
                   key=lambda x: x[0] or now)
    if not dated:
        return None
    last_t, last = dated[-1]
    pt = _point(last)
    if pt is None:
        return None
    closed = parse_time(e.get("closed"))
    sources = [s for s in e.get("sources") or [] if isinstance(s, dict)]
    mag = last.get("magnitudeValue")
    attrs = {
        "source": "eonet", "hazard": CATEGORIES.get(cats[0], "other"), "code": cats[0],
        "name": e.get("title") or "", "description": e.get("description") or "",
        "alert_level": None, "event_id": e["id"],
        "severity_text": f"{mag:g} {last.get('magnitudeUnit') or ''}".strip()
        if isinstance(mag, int | float) else "",
        "unit": last.get("magnitudeUnit"), "current": closed is None,
        "t_start": iso(dated[0][0]), "t_end": iso(closed or last_t),
        "updated_at": iso(last_t), "precision": "point", "fixes": len(geoms),
        "agency": ",".join(s.get("id") or "" for s in sources),
        "url": next((s.get("url") for s in sources if s.get("url")), None) or e.get("link"),
        "api_url": e.get("link"),
    }
    if pt[2] is not None:
        attrs["polygon"] = pt[2]
    return Observation(
        id=f"hazard:eonet:{e['id']}",
        version=f"{last.get('date')}:{e.get('closed') or ''}",
        type="hazard", ts=observed_ts(last_t, now), lat=pt[1], lon=pt[0], source="eonet",
        value=float(mag) if isinstance(mag, int | float) else None, attrs=attrs,
    )


@register
class EonetCollector(PollCollector):
    name = "eonet"
    domain = "hazards"
    licence = "NASA EONET - NASA open data (public domain); upstream sources credited"
    commercial_ok = True
    default_interval_s = 1800.0
    poll_timeout_s = 90.0

    async def poll(self, ctx: CollectorContext) -> Batch:
        now = utcnow()
        params = {"status": self.cfg.get("status", "open"),
                  "days": int(self.cfg.get("days", 30))}
        r = await ctx.http.get(self.cfg.get("url", API_URL), params=params, timeout=60)
        r.raise_for_status()
        events = (r.json() or {}).get("events")
        if not isinstance(events, list):
            raise TypeError("unexpected EONET payload (no events list)")
        exclude = tuple(self.cfg.get("exclude_categories", DEFAULT_EXCLUDE))
        pattern = self.cfg.get("exclude_title_re", DEFAULT_EXCLUDE_TITLE)
        exclude_title = re.compile(pattern) if pattern else None
        batch = Batch(source=self.name)
        skipped = 0
        for e in events:
            try:
                obs = parse_event(e, now, exclude, exclude_title)
            except (KeyError, ValueError, TypeError, AttributeError):
                skipped += 1
                continue
            if obs is not None:
                batch.observations.append(obs)
        if skipped:
            log.warning("eonet: skipped %d malformed event(s)", skipped)
        return batch

