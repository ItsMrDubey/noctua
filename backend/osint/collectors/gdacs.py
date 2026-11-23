"""GDACS (Global Disaster Alert and Coordination System, EC JRC + UN OCHA) alerts:
earthquakes, tropical cyclones, floods, volcanoes, droughts and forest fires, worldwide.

Endpoint (verified 2026-11-23, no key):
https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?eventlist=EQ;TC;FL;VO;DR;WF
&fromDate=YYYY-MM-DD&toDate=YYYY-MM-DD&alertlevel=Green;Orange;Red&pagenumber=N
-> GeoJSON FeatureCollection of Point features, or HTTP 204 with an empty body when there
are none / past the last page (100 per page; events overlapping the window,
so ongoing droughts / cyclones are included). properties: eventtype, eventid, episodeid, name,
htmldescription, alertlevel, alertscore, iscurrent, country, iso3, affectedcountries[{iso2,
iso3, countryname}], fromdate, todate, datemodified (naive UTC), glide, source,
severitydata{severity, severitytext, severityunit}, url{report, geometry, details}.

One ``hazard`` observation per GDACS event (see ``osint.core.hazards``); version = the
episode + alert level + datemodified, so every revision is stored once. The per-event alert
polygons (``url.geometry``, ~200 kB for a cyclone) are not fetched; the URL is kept in attrs.
"""

import logging
from datetime import timedelta

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.geo import utcnow
from osint.core.hazards import iso, observed_ts, parse_time
from osint.core.models import Batch, Observation

log = logging.getLogger(__name__)

API_URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
EVENT_TYPES = {"EQ": "earthquake", "TC": "tropical_cyclone", "FL": "flood", "VO": "volcano",
               "DR": "drought", "WF": "wildfire", "TS": "tsunami"}
PAGE_SIZE = 100  # fixed server-side


def parse_feature(f: dict, now) -> Observation | None:
    """One GDACS feature -> Observation. None when it has no id/point; raises
    KeyError/ValueError/TypeError when malformed."""
    p = f.get("properties") or {}
    coords = (f.get("geometry") or {}).get("coordinates") or []
    etype = str(p.get("eventtype") or "").upper()
    if len(coords) < 2 or not etype or p.get("eventid") is None:
        return None
    updated = parse_time(p.get("datemodified"))
    alert = str(p.get("alertlevel") or "").casefold() or None
    sev = p.get("severitydata") or {}
    affected = [c for c in p.get("affectedcountries") or [] if isinstance(c, dict)]
    urls = p.get("url") or {}
    return Observation(
        id=f"hazard:gdacs:{etype}:{int(p['eventid'])}",
        version=f"{p.get('episodeid')}:{alert}:{p.get('datemodified')}",
        type="hazard",
        ts=observed_ts(updated, now),
        lat=float(coords[1]),
        lon=float(coords[0]),
        source="gdacs",
        value=float(sev["severity"]) if isinstance(sev.get("severity"), int | float) else None,
        attrs={
            "source": "gdacs", "hazard": EVENT_TYPES.get(etype, "other"), "code": etype,
            "event_id": p.get("eventid"), "episode_id": p.get("episodeid"),
            "name": p.get("name") or p.get("eventname") or "",
            "event_name": p.get("eventname") or "",
            "description": p.get("htmldescription") or p.get("description") or "",
            "alert_level": alert, "alert_score": p.get("alertscore"),
            "severity_text": sev.get("severitytext") or "", "unit": sev.get("severityunit"),
            "current": str(p.get("iscurrent")).casefold() == "true",
            "country": p.get("country") or "", "iso3": p.get("iso3") or "",
            "iso3s": [c.get("iso3") for c in affected if c.get("iso3")],
            "iso2": [c.get("iso2") for c in affected if c.get("iso2")],
            "t_start": iso(parse_time(p.get("fromdate"))),
            "t_end": iso(parse_time(p.get("todate"))),
            "updated_at": iso(updated), "glide": p.get("glide") or "",
            "agency": p.get("source") or "", "precision": "point",
            "url": urls.get("report"), "geometry_url": urls.get("geometry"),
        },
    )


@register
class GdacsCollector(PollCollector):
    name = "gdacs"
    domain = "hazards"
    licence = "GDACS (EC JRC / UN OCHA), free use with attribution"
    commercial_ok = True
    default_interval_s = 1800.0
    poll_timeout_s = 120.0

    async def poll(self, ctx: CollectorContext) -> Batch:
        now = utcnow()
        days = float(self.cfg.get("days", 3))
        params = {
            "eventlist": ";".join(self.cfg.get("event_types") or list(EVENT_TYPES)[:6]),
            "fromDate": (now - timedelta(days=days)).date().isoformat(),
            "toDate": (now + timedelta(days=1)).date().isoformat(),
            "alertlevel": ";".join(self.cfg.get("alert_levels") or ["Green", "Orange", "Red"]),
        }
        batch = Batch(source=self.name)
        seen: set[str] = set()
        skipped = 0
        for page in range(1, int(self.cfg.get("max_pages", 10)) + 1):
            r = await ctx.http.get(self.cfg.get("url", API_URL),
                                   params={**params, "pagenumber": page}, timeout=60)
            r.raise_for_status()
            if r.status_code == 204 or not r.content:  # no (more) events: empty body
                break
            features = (r.json() or {}).get("features") or []
            for f in features:
                try:
                    obs = parse_feature(f, now)
                except (KeyError, ValueError, TypeError, AttributeError):
                    skipped += 1
                    continue
                if obs is not None and obs.id not in seen:
                    seen.add(obs.id)
                    batch.observations.append(obs)
            if len(features) < PAGE_SIZE:
                break
        if skipped:
            log.warning("gdacs: skipped %d malformed feature(s)", skipped)
        return batch
