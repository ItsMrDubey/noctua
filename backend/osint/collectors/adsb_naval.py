"""Low-rate targeted ADS-B collection for the carrier model (``osint.naval``): carrier-
associated and maritime-patrol aircraft that the military feed misses.

Three request kinds, each paced separately (verified 2026-11-23):

* **type discovery** - adsb.lol ``/v2/type/{ICAO type}`` (adsb.fi has no type endpoint:
  HTTP 400). adsb.lol allows ~500 requests/day per client (``x-ratelimit-limit: 500;
  w=86400``) and that budget is SHARED with the ``adsb`` collector's ``/v2/mil`` polls, so
  this collector takes at most ``type_budget_per_day`` (default 48) and stops whenever the
  remaining shared budget falls below ``lol_reserve``. Types are polled round-robin,
  weighted by priority.
* **regional sweep** - adsb.fi ``/api/v2/lat/{lat}/lon/{lon}/dist/{nm}`` (all aircraft) over
  carrier operating areas not already swept by ``adsb_signals``, every
  ``region_interval_s``, ``spacing_s`` apart (adsb.fi: 1 request/s).
* **follow** - adsb.fi ``/api/v2/hex/{a},{b},...`` (comma-separated, verified) every poll for
  captured aircraft seen in the last ``follow_hours``, so a discovered COD / helicopter gets a
  continuous track (launch / recovery reconstruction needs consecutive reports).

Only aircraft that are NOT flagged military are kept (``osint.naval.capture``): flagged ones
are already recorded by ``adsb``. Positions go to the lake as source
``adsb_naval:<endpoint>``; tracks carry ``extra.naval_capture`` (why it was captured).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from osint.collectors.adsb import USER_AGENT, EndpointError, _num, pacing_delay, parse_aircraft
from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.models import Batch
from osint.enrich.military import MilDb
from osint.naval.capture import NavalCapture

log = logging.getLogger(__name__)

LOL_BASE = "https://api.adsb.lol/v2"
FI_BASE = "https://opendata.adsb.fi/api/v2"
# ICAO type -> priority (polled this many times per round-robin cycle)
DEFAULT_TYPES: dict[str, int] = {
    "V22": 3, "H60": 2, "E2": 3, "C2": 1, "F18S": 2, "F18H": 1, "E18G": 1, "F35": 1,
    "RAFL": 2, "EH10": 2, "NH90": 1, "KA27": 1, "J15": 1, "SU33": 1,
    "P8": 1, "P1": 1, "ATL2": 1, "Q4": 1,
}
# Carrier operating areas NOT covered by adsb_signals' GNSS watch regions (which already
# include the Eastern Med, Red Sea, Persian Gulf, Black Sea, Baltic, Korea). Radius NM <= 250.
DEFAULT_REGIONS: list[dict[str, Any]] = [
    {"name": "Virginia Capes / Cherry Point OPAREA", "lat": 35.5, "lon": -73.5, "dist": 250},
    {"name": "Jacksonville OPAREA", "lat": 30.5, "lon": -79.0, "dist": 200},
    {"name": "SOCAL OPAREA", "lat": 32.0, "lon": -119.5, "dist": 250},
    {"name": "Hawaii OPAREA", "lat": 21.0, "lon": -158.5, "dist": 250},
    {"name": "Gulf of Oman / N Arabian Sea", "lat": 23.5, "lon": 60.0, "dist": 250},
    {"name": "Philippine Sea / Okinawa", "lat": 25.5, "lon": 129.0, "dist": 250},
    {"name": "South China Sea / Luzon Strait", "lat": 18.0, "lon": 118.0, "dist": 250},
    {"name": "Central Mediterranean / Ionian", "lat": 37.0, "lon": 18.0, "dist": 250},
]


def _aircraft(data: dict) -> tuple[float, list[dict]]:
    now = data.get("now")
    now_s = float(now) if isinstance(now, int | float) else time.time()
    if now_s > 1e11:
        now_s /= 1000.0
    ac = data.get("ac") if data.get("ac") is not None else data.get("aircraft")
    return now_s, [a for a in (ac or []) if isinstance(a, dict)]


@register
class AdsbNavalCollector(PollCollector):
    name = "adsb_naval"
    domain = "aviation"
    licence = "adsb.lol ODbL; adsb.fi open data non-commercial"
    commercial_ok = False
    default_interval_s = 60.0
    poll_timeout_s = 90.0

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.lol_base = str(cfg.get("lol_base") or LOL_BASE).rstrip("/")
        self.fi_base = str(cfg.get("fi_base") or FI_BASE).rstrip("/")
        types = cfg.get("types") or DEFAULT_TYPES
        if isinstance(types, list):
            types = {t: 1 for t in types}
        self.schedule = [t for t, n in types.items() for _ in range(max(1, int(n)))]
        self.type_budget = int(cfg.get("type_budget_per_day", 48))
        self.lol_reserve = int(cfg.get("lol_reserve", 150))
        self.regions = list(cfg.get("regions") or DEFAULT_REGIONS)
        self.region_interval_s = float(cfg.get("region_interval_s", 300))
        self.spacing_s = float(cfg.get("spacing_s", 1.5))
        self.follow_hours = float(cfg.get("follow_hours", 6))
        self.follow_max = int(cfg.get("follow_max", 60))
        self.follow_chunk = int(cfg.get("follow_chunk", 30))
        self.user_agent = str(cfg.get("user_agent") or USER_AGENT)
        self.sleep = asyncio.sleep          # injectable for tests
        self._clock = time.monotonic
        self._wall = time.time
        self.capture: NavalCapture | None = cfg.get("_capture")  # tests inject one
        self._mil_db: MilDb | None = cfg.get("_mil_db")
        self._loaded = False
        # state (persisted in the collector cursor)
        self.day = ""
        self.lol_used = 0
        self.rr = 0
        self.follow: dict[str, float] = {}  # hex -> wall time last captured
        # runtime pacing
        self.lol_next = 0.0
        self.lol_remaining: float | None = None
        self.region_next = 0.0
        self.stats: dict[str, int] = {"type_requests": 0, "region_requests": 0,
                                      "follow_requests": 0, "captured": 0}

    # ------------------------------------------------------------------ state

    def _load(self, ctx: CollectorContext) -> None:
        cur = ctx.get_cursor(self.name) or {}
        self.day = str(cur.get("day") or "")
        self.lol_used = int(cur.get("lol_used") or 0)
        self.rr = int(cur.get("rr") or 0)
        self.follow = {str(k): float(v) for k, v in (cur.get("follow") or {}).items()}

    def _save(self, ctx: CollectorContext) -> None:
        ctx.set_cursor(self.name, {"day": self.day, "lol_used": self.lol_used, "rr": self.rr,
                                   "follow": self.follow})

    def status(self) -> dict:
        return {"day": self.day, "adsb_lol_type_requests_today": self.lol_used,
                "type_budget_per_day": self.type_budget,
                "adsb_lol_shared_remaining": self.lol_remaining,
                "following": len(self.follow), **self.stats}

    # ------------------------------------------------------------------ fetch

    async def _get(self, ctx: CollectorContext, url: str) -> httpx.Response:
        try:
            r = await ctx.http.get(url, timeout=20, headers={"User-Agent": self.user_agent})
        except httpx.HTTPError as e:
            raise EndpointError(f"{type(e).__name__}: {e}") from e
        if r.status_code == 429:
            raise EndpointError(f"HTTP 429 for {url}", retry_after_s=_num(
                r.headers.get("retry-after")) or pacing_delay(r.headers) or 3600.0)
        if r.status_code >= 400:
            raise EndpointError(f"HTTP {r.status_code} for {url}",
                                denied=r.status_code in (401, 403))
        return r

    def _type_due(self, now: float) -> bool:
        if not self.schedule or self.type_budget <= 0 or self.lol_used >= self.type_budget:
            return False
        if self.lol_remaining is not None and self.lol_remaining <= self.lol_reserve:
            return False
        return now >= self.lol_next

    async def _type_query(self, ctx, now: float, errors: list[str]) -> list[tuple]:
        t = self.schedule[self.rr % len(self.schedule)]
        self.rr = (self.rr + 1) % len(self.schedule)
        self.lol_used += 1
        self.stats["type_requests"] += 1
        spacing = 86400.0 / max(1, self.type_budget)
        try:
            r = await self._get(ctx, f"{self.lol_base}/type/{t}")
        except EndpointError as e:
            self.lol_next = now + max(spacing, e.retry_after_s or 0.0,
                                      6 * 3600.0 if e.denied else 0.0)
            errors.append(f"adsb.lol type {t}: {e}")
            return []
        self.lol_remaining = _num(r.headers.get("x-ratelimit-remaining"))
        self.lol_next = now + max(spacing, pacing_delay(r.headers))
        now_s, ac = _aircraft(r.json())
        return [("adsb.lol", now_s, a) for a in ac]

    async def _fi(self, ctx, path: str, kind: str, errors: list[str]) -> list[tuple] | None:
        """None = rate limited (stop this cycle's adsb.fi requests)."""
        self.stats[f"{kind}_requests"] += 1
        try:
            r = await self._get(ctx, f"{self.fi_base}{path}")
        except EndpointError as e:
            errors.append(f"adsb.fi {path}: {e}")
            return None if e.retry_after_s else []
        now_s, ac = _aircraft(r.json())
        return [("adsb.fi", now_s, a) for a in ac]

    # ------------------------------------------------------------------- poll

    async def poll(self, ctx: CollectorContext) -> Batch:
        if not self._loaded:
            self._load(ctx)
            if self.capture is None:
                self.capture = await asyncio.to_thread(NavalCapture.from_config_dir,
                                                       ctx.settings.config_dir)
            if self._mil_db is None:
                self._mil_db = await asyncio.to_thread(
                    MilDb.from_csv, ctx.settings.reference_dir / "plane-alert-mil.csv")
            self._loaded = True
        now, wall = self._clock(), self._wall()
        today = datetime.fromtimestamp(wall, UTC).date().isoformat()
        if today != self.day:
            self.day, self.lol_used = today, 0
        errors: list[str] = []
        got: list[tuple] = []
        attempted = 0
        if self._type_due(now):
            attempted += 1
            got += await self._type_query(ctx, now, errors)
        fi_paths: list[tuple[str, str]] = []
        if now >= self.region_next and self.regions:
            self.region_next = now + self.region_interval_s
            fi_paths += [("region", f"/lat/{r['lat']}/lon/{r['lon']}/dist/{int(r.get('dist', 250))}")
                         for r in self.regions]
        cutoff = wall - self.follow_hours * 3600
        self.follow = {h: t for h, t in self.follow.items() if t >= cutoff}
        hexes = sorted(self.follow, key=lambda h: -self.follow[h])[: self.follow_max]
        fi_paths += [("follow", "/hex/" + ",".join(hexes[i:i + self.follow_chunk]))
                     for i in range(0, len(hexes), self.follow_chunk)]
        for i, (kind, path) in enumerate(fi_paths):
            if i or attempted:
                await self.sleep(self.spacing_s)
            attempted += 1
            res = await self._fi(ctx, path, kind, errors)
            if res is None:
                break
            got += res
        if attempted and len(errors) >= attempted:
            self._save(ctx)
            raise RuntimeError("adsb_naval: every request failed: " + "; ".join(errors[:3]))
        if errors:
            log.debug("adsb_naval: %d request(s) failed: %s", len(errors), "; ".join(errors[:3]))
        batch = self._batch(got, wall)
        self._save(ctx)
        return batch

    def _batch(self, got: list[tuple], wall: float) -> Batch:
        best: dict[str, tuple[float, str, float, dict, str]] = {}
        assert self.capture is not None
        for api, now_s, ac in got:
            why = self.capture.reason(ac)
            if why is None:
                continue
            hx = str(ac.get("hex") or "").strip().lower()
            if not hx or hx.startswith("~"):
                continue
            try:
                age = float(ac.get("seen_pos", ac.get("seen", 1e9)) or 0)
            except (TypeError, ValueError):
                continue
            if hx not in best or age < best[hx][0]:
                best[hx] = (age, api, now_s, ac, why)
        batch = Batch(source=self.name)
        for hx, (_, api, now_s, ac, why) in best.items():
            try:
                parsed = parse_aircraft(ac, now_s, self._mil_db or MilDb({}), f"adsb_naval:{api}")
            except (ValueError, TypeError, KeyError):
                continue
            if parsed is None:
                continue
            pos, track = parsed
            track.extra = {**track.extra, "naval_capture": why}
            batch.tracks.append(track)
            if pos is not None:
                batch.positions.append(pos)
            self.follow[hx] = wall
        self.stats["captured"] += len(batch.tracks)
        return batch
