"""NOAA SWPC space weather: Kp (3-hourly and 1-minute estimate), NOAA G/S/R scales, alerts,
solar wind, X-ray flares, F10.7, and the OVATION aurora nowcast.

All products are free public JSON (no key). Writes ``signals/spaceweather.json`` every poll
and ``signals/aurora.json`` every ``aurora_interval_s`` (the source file is ~1 MB). A product
that fails keeps its previous value; the poll fails only when every product failed.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.models import Batch
from osint.signals import spaceweather as sw
from osint.signals.cache import SignalReader, write_signal

log = logging.getLogger(__name__)

BASE = "https://services.swpc.noaa.gov"
PRODUCTS = {
    "kp": "/products/noaa-planetary-k-index.json",
    "kp_1m": "/json/planetary_k_index_1m.json",
    "scales": "/products/noaa-scales.json",
    "alerts": "/products/alerts.json",
    "wind_speed": "/products/summary/solar-wind-speed.json",
    "wind_mag": "/products/summary/solar-wind-mag-field.json",
    "flares": "/json/goes/primary/xray-flares-latest.json",
    "f107": "/products/summary/10cm-flux.json",
}
AURORA = "/json/ovation_aurora_latest.json"


@register
class SwpcCollector(PollCollector):
    name = "swpc"
    domain = "space"
    licence = "NOAA SWPC (US Government work, public domain)"
    commercial_ok = True
    default_interval_s = 300.0

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.base = str(cfg.get("base") or BASE).rstrip("/")
        self.aurora_interval_s = float(cfg.get("aurora_interval_s", 1800))
        self._aurora_at = 0.0
        self._last: dict | None = None

    async def _get(self, ctx: CollectorContext, path: str):
        r = await ctx.http.get(self.base + path, timeout=45)
        r.raise_for_status()
        return r.json()

    async def poll(self, ctx: CollectorContext) -> Batch:
        data_dir = ctx.settings.data_dir
        if self._last is None:
            self._last = SignalReader(data_dir).get("spaceweather") or {}
        names = list(PRODUCTS)
        res = await asyncio.gather(*(self._get(ctx, PRODUCTS[n]) for n in names),
                                   return_exceptions=True)
        raw = {}
        errors = []
        for n, v in zip(names, res, strict=True):
            if isinstance(v, BaseException):
                if not isinstance(v, (httpx.HTTPError, ValueError)):
                    raise v
                errors.append(f"{n}: {type(v).__name__}: {v}")
            else:
                raw[n] = v
        if not raw:
            raise RuntimeError("swpc: every product failed: " + "; ".join(errors))
        for e in errors:
            log.info("swpc product failed: %s", e)
        now = time.time()
        snap = dict(self._last)
        if "kp" in raw:
            snap["kp"] = sw.parse_kp(raw["kp"])[-56:]  # 7 days of 3-hour values
        if "kp_1m" in raw:
            series = sw.parse_kp(raw["kp_1m"])
            snap["kp_now"] = series[-1] if series else None
            snap["kp_1m"] = series[-180:]
        if "scales" in raw:
            snap["scales"] = sw.parse_scales(raw["scales"])
        if "alerts" in raw:
            snap["alerts"] = sw.parse_alerts(raw["alerts"])
        if "wind_speed" in raw or "wind_mag" in raw:
            snap["solar_wind"] = sw.solar_wind(raw.get("wind_speed"), raw.get("wind_mag"))
        if "flares" in raw:
            snap["flares"] = sw.parse_flares(raw["flares"])
        if "f107" in raw:
            f = sw._first(raw["f107"])
            snap["f107"] = {"flux": f.get("flux"), "at": sw._ts(f.get("time_tag"))}
        kp_now = (snap.get("kp_now") or [None, None])[1]
        if kp_now is None and snap.get("kp"):
            kp_now = snap["kp"][-1][1]
        snap["g_now"] = sw.g_scale(kp_now) if kp_now is not None else None
        snap["updated"] = now
        snap["errors"] = errors
        self._last = snap
        write_signal(data_dir, "spaceweather", snap)
        if now - self._aurora_at >= self.aurora_interval_s:
            try:
                grid = sw.aurora_grid(await self._get(ctx, AURORA))
            except (httpx.HTTPError, ValueError) as exc:
                log.info("swpc aurora nowcast failed: %s", exc)
            else:
                if grid is not None:
                    self._aurora_at = now
                    write_signal(data_dir, "aurora", {"updated": now, **grid})
        return Batch(source=self.name)
