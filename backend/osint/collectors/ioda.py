"""IODA (Georgia Tech) country-level internet outage events, every 10 minutes.

``GET /v2/outages/events?entityType=country&from=<now - window>&until=<now>``: free, no key.
Writes ``signals/outages.json`` (per-country roll-up for map shading + the raw events) and one
``outage`` lake observation per event at the country label point (re-versioned while the
event's end keeps moving). Severity and event thresholds: ``osint.signals.outages``.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.models import Batch, Observation
from osint.signals.cache import write_signal
from osint.signals.countries import Countries, get_countries
from osint.signals.outages import by_country, notable, parse_events

log = logging.getLogger(__name__)

API = "https://api.ioda.inetintel.cc.gatech.edu/v2"
VERSION_S = 600


def outage_observation(e: dict, lat: float, lon: float, cfg: dict) -> Observation:
    until = e["until"]
    return Observation(
        id=f"outage:{e['cc']}:{e['datasource']}:{e['from']}", type="outage",
        version=str(until // VERSION_S), ts=datetime.fromtimestamp(until, UTC), lat=lat,
        lon=lon, source="ioda", value=e["score"],
        attrs={**{k: e[k] for k in ("cc", "name", "datasource", "method", "from", "until",
                                    "weight", "severity", "ongoing")},
               "notable": notable(e, cfg)})


@register
class IodaCollector(PollCollector):
    name = "ioda"
    domain = "cyber"
    licence = "IODA (Georgia Tech) data; free for research, attribution required"
    commercial_ok = False
    default_interval_s = 600.0

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.api = str(cfg.get("api") or API).rstrip("/")
        self.window_h = float(cfg.get("window_h", 24))
        self.weights = dict(cfg.get("weights") or {})
        self.countries: Countries | None = None

    async def poll(self, ctx: CollectorContext) -> Batch:
        if self.countries is None:
            self.countries = get_countries()
        now = int(time.time())
        r = await ctx.http.get(f"{self.api}/outages/events", timeout=60, params={
            "entityType": "country", "from": now - int(self.window_h * 3600), "until": now,
            "format": "ioda", "limit": 1000})
        r.raise_for_status()
        events = parse_events(r.json(), now, self.cfg, self.weights)
        batch = Batch(source=self.name)
        located = []
        for e in events:
            pt = self.countries.point(e["cc"])
            if pt is None:
                continue
            e["lat"], e["lon"] = pt[0], pt[1]
            located.append(e)
            batch.observations.append(outage_observation(e, pt[0], pt[1], self.cfg))
        countries = by_country(located)
        for c in countries:
            c["lat"], c["lon"] = self.countries.point(c["cc"])[:2]
        data_dir = getattr(ctx.settings, "data_dir", None)
        if data_dir is not None:
            write_signal(data_dir, "outages", {"updated": now, "window_h": self.window_h,
                                               "countries": countries, "events": located})
        return batch
