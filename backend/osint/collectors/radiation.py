"""Ambient gamma dose rate: BfS ODL (Germany) and Safecast realtime devices, every 15 min.

Writes ``signals/radiation.json`` (latest reading per station) and one ``radiation`` lake
observation per new reading (id ``rad:<source>:<station>``, version = reading time), from
which ``osint.signals.job`` derives each station's baseline and flags anomalies.
EURDEP (the EU exchange platform) has no public machine API without registration: skipped.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

import httpx

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.models import Batch, Observation
from osint.signals.cache import write_signal
from osint.signals.radiation import DEFAULTS, parse_bfs, parse_safecast

log = logging.getLogger(__name__)

BFS_URL = ("https://www.imis.bfs.de/ogc/opendata/ows?service=WFS&version=1.1.0"
           "&request=GetFeature&typeName=opendata:odlinfo_odl_1h_latest"
           "&outputFormat=application/json"
           "&propertyName=id,name,site_status,end_measure,value,value_cosmic,geom")
SAFECAST_URL = "https://tt.safecast.org/devices"


def reading_observation(r: dict) -> Observation:
    return Observation(
        id=f"rad:{r['id']}", type="radiation", version=r["ts"].isoformat(), ts=r["ts"],
        lat=r["lat"], lon=r["lon"], source=r["source"], value=r["value"],
        attrs={k: r[k] for k in ("name", "country", "unit", "cosmic", "cpm", "tube")
               if r.get(k) is not None})


@register
class RadiationCollector(PollCollector):
    name = "radiation"
    domain = "sensor"
    licence = "BfS ODL: dl-de/by-2-0; Safecast: CC0"
    commercial_ok = True
    default_interval_s = 900.0

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        nets = cfg.get("networks") or {}
        self.urls = {"bfs": str(nets.get("bfs", BFS_URL) or ""),
                     "safecast": str(nets.get("safecast", SAFECAST_URL) or "")}
        self.max_age_h = float(cfg.get("max_age_h", DEFAULTS["max_age_h"]))

    async def _get(self, ctx: CollectorContext, url: str):
        r = await ctx.http.get(url, timeout=90)
        r.raise_for_status()
        return r.json()

    async def poll(self, ctx: CollectorContext) -> Batch:
        nets = [n for n, u in self.urls.items() if u]
        res = await asyncio.gather(*(self._get(ctx, self.urls[n]) for n in nets),
                                   return_exceptions=True)
        now = datetime.now(UTC)
        readings, status, errors = [], {}, []
        for n, v in zip(nets, res, strict=True):
            if isinstance(v, BaseException):
                if not isinstance(v, (httpx.HTTPError, ValueError)):
                    raise v
                errors.append(f"{n}: {type(v).__name__}: {v}")
                status[n] = {"ok": False, "error": str(v)[:200]}
                continue
            rows = parse_bfs(v) if n == "bfs" else parse_safecast(v, now, self.max_age_h)
            if n == "bfs":
                rows = [r for r in rows if (now - r["ts"]).total_seconds()
                        <= self.max_age_h * 3600]
            readings += rows
            status[n] = {"ok": True, "stations": len(rows)}
        if nets and len(errors) == len(nets):
            raise RuntimeError("radiation: every network failed: " + "; ".join(errors))
        for e in errors:
            log.warning("radiation network failed: %s", e)
        batch = Batch(source=self.name, observations=[reading_observation(r) for r in readings])
        data_dir = getattr(ctx.settings, "data_dir", None)
        if data_dir is not None:
            write_signal(data_dir, "radiation", {
                "updated": time.time(), "networks": status,
                "stations": [{**r, "ts": r["ts"].isoformat()} for r in readings]})
        return batch
