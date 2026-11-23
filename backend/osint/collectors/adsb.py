"""Global military ADS-B from several free aggregators, merged and de-duplicated by ICAO hex.

Endpoints (``sources.yaml`` -> ``adsb.endpoints``; a bare URL means a readsb ``/v2`` base):

* ``readsb`` — readsb-compatible ``<base>/mil`` (adsb.lol, adsb.fi opendata). Polled every
  cycle, concurrently; the freshest record per hex wins.
* ``opensky`` — OpenSky Network anonymous ``/states/all`` (no military flag: filtered to the
  hexes in the plane-alert military DB). ``fallback: true`` = only polled when no primary
  endpoint returned data this cycle; ``min_interval_s`` keeps it inside the anonymous
  credit budget (400/day, 4 per global call).

Per-endpoint politeness: a failing endpoint backs off exponentially (``max_backoff_s``); 401/
403 (access revoked / approval required, e.g. airplanes.live since 2026) backs off for
``denied_backoff_s``; 429 honours Retry-After; ``x-ratelimit-remaining`` / ``-reset`` headers
pace the endpoint so the daily budget lasts until the reset. A failure is logged at WARNING
once when an endpoint starts failing (and once when it recovers), then at DEBUG.
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.models import Batch, Position, TrackInfo
from osint.enrich.mil_types import get_mil_category_from_type, is_high_priority
from osint.enrich.military import MilDb

log = logging.getLogger(__name__)

# Verified with curl on 2026-11-23: adsb.lol and adsb.fi /v2/mil 200; OpenSky anonymous
# /states/all 200; airplanes.live 403 ("contact us" - access now needs their approval).
DEFAULT_ENDPOINTS: dict[str, Any] = {
    "adsb.lol": "https://api.adsb.lol/v2",
    "adsb.fi": "https://opendata.adsb.fi/api/v2",
    "opensky": {"url": "https://opensky-network.org/api", "kind": "opensky",
                "fallback": True, "min_interval_s": 900},
}
USER_AGENT = "osint-poc/0.1 (non-commercial OSINT research; military ADS-B poll)"
_M_TO_FT = 3.28084
_MS_TO_KTS = 1.943844


@dataclass
class Endpoint:
    name: str
    url: str
    kind: str = "readsb"  # readsb | opensky
    fallback: bool = False
    min_interval_s: float = 0.0
    # state
    failures: int = 0
    not_before: float = 0.0  # monotonic; skipped until then
    last_ok: float | None = None
    last_attempt: float | None = None
    warned: bool = False

    @classmethod
    def from_cfg(cls, name: str, spec: Any) -> "Endpoint":
        if isinstance(spec, str):
            return cls(name=name, url=spec.rstrip("/"))
        return cls(name=name, url=str(spec["url"]).rstrip("/"),
                   kind=str(spec.get("kind") or "readsb"),
                   fallback=bool(spec.get("fallback", False)),
                   min_interval_s=float(spec.get("min_interval_s") or 0))


class EndpointError(Exception):
    """A failed fetch with the delay (s) the endpoint asked for, if any."""

    def __init__(self, msg: str, retry_after_s: float | None = None,
                 denied: bool = False) -> None:
        super().__init__(msg)
        self.retry_after_s = retry_after_s
        self.denied = denied


def _num(v: Any) -> float | None:
    try:
        return float(str(v).split(",")[0].strip())
    except (TypeError, ValueError):
        return None


def pacing_delay(headers: httpx.Headers) -> float:
    """Seconds to wait before the next call so a rate-limit budget lasts until its reset
    (``x-ratelimit-remaining`` / ``x-ratelimit-reset``, adsb.lol style); 0 if no headers."""
    remaining = _num(headers.get("x-ratelimit-remaining"))
    reset = _num(headers.get("x-ratelimit-reset"))
    if remaining is None or reset is None or reset <= 0:
        return 0.0
    return reset if remaining <= 0 else reset / remaining


def opensky_to_readsb(state: list, now_s: float) -> dict | None:
    """One OpenSky state vector -> a readsb-style aircraft dict (``parse_aircraft`` input).
    [icao24, callsign, origin_country, time_position, last_contact, lon, lat, baro_alt_m,
    on_ground, velocity_ms, true_track, vertical_rate, sensors, geo_alt_m, squawk, spi,
    position_source]"""
    if len(state) < 11 or not state[0]:
        return None
    ac: dict[str, Any] = {"hex": str(state[0]), "flight": state[1] or ""}
    if state[5] is not None and state[6] is not None:
        ac["lon"], ac["lat"] = state[5], state[6]
    if state[8]:
        ac["alt_baro"] = "ground"
    elif isinstance(state[7], int | float):
        ac["alt_baro"] = round(state[7] * _M_TO_FT)
    elif len(state) > 13 and isinstance(state[13], int | float):
        ac["alt_geom"] = round(state[13] * _M_TO_FT)
    if isinstance(state[9], int | float):
        ac["gs"] = round(state[9] * _MS_TO_KTS, 1)
    if isinstance(state[10], int | float):
        ac["track"] = state[10]
    if len(state) > 14 and state[14]:
        ac["squawk"] = state[14]
    seen = state[3] if isinstance(state[3], int | float) else state[4]
    if isinstance(seen, int | float):
        ac["seen_pos"] = max(0.0, now_s - seen)
    return ac


def parse_aircraft(
    ac: dict, now_s: float, mil_db: MilDb, source: str
) -> tuple[Position | None, TrackInfo] | None:
    hex_code = (ac.get("hex") or "").strip().lower()
    if not hex_code or hex_code.startswith("~"):  # "~" = non-ICAO (TIS-B) address
        return None
    entry = mil_db.get(hex_code) or {}
    ac_type = ac.get("t") or entry.get("icao_type") or ""
    type_is_mil, type_category = get_mil_category_from_type(ac_type)
    is_mil = bool((ac.get("dbFlags") or 0) & 1) or bool(entry) or type_is_mil
    track_id = f"air:{hex_code}"
    extra = {
        "desc": ac.get("desc") or entry.get("type") or "",
        "role": entry.get("tag1") or "",
        "squawk": ac.get("squawk") or "",
    }
    track = TrackInfo(
        track_id=track_id,
        kind="air",
        ident=hex_code,
        name=(ac.get("flight") or "").strip() or None,
        reg=ac.get("r") or entry.get("reg") or None,
        type_code=ac_type or None,
        operator=entry.get("operator") or None,
        category=entry.get("category") or type_category or None,
        is_mil=is_mil,
        high_priority=is_mil and is_high_priority(ac_type),
        extra={k: v for k, v in extra.items() if v},
    )
    lat, lon = ac.get("lat"), ac.get("lon")
    if lat is None or lon is None:
        return None, track
    alt = ac.get("alt_baro")
    if alt == "ground":
        alt_ft = 0
    else:
        alt = alt if isinstance(alt, (int, float)) else ac.get("alt_geom")
        alt_ft = int(alt) if isinstance(alt, (int, float)) else None
    age = float(ac.get("seen_pos", ac.get("seen", 0)) or 0)
    pos = Position(
        track_id=track_id, kind="air", ts=datetime.fromtimestamp(now_s - age, UTC),
        lat=float(lat), lon=float(lon), source=source, alt_ft=alt_ft,
        speed_kts=ac.get("gs"), heading_deg=ac.get("track"),
    )
    return pos, track


@register
class AdsbCollector(PollCollector):
    name = "adsb"
    domain = "aviation"
    licence = "mixed: adsb.lol ODbL; adsb.fi, airplanes.live, OpenSky non-commercial"
    commercial_ok = False
    default_interval_s = 30.0

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self._mil_db: MilDb | None = None
        specs = cfg.get("endpoints") or DEFAULT_ENDPOINTS
        self.endpoints = [Endpoint.from_cfg(n, s) for n, s in specs.items()
                          if not (isinstance(s, dict) and s.get("enabled") is False)]
        self.max_backoff_s = float(cfg.get("max_backoff_s", 1800))
        self.denied_backoff_s = float(cfg.get("denied_backoff_s", 6 * 3600))
        self.user_agent = str(cfg.get("user_agent") or USER_AGENT)
        self._clock = time.monotonic

    # ------------------------------------------------------------------ fetch

    async def _get(self, ctx: CollectorContext, ep: Endpoint, url: str) -> httpx.Response:
        try:
            r = await ctx.http.get(url, timeout=20, headers={"User-Agent": self.user_agent})
        except httpx.HTTPError as e:
            raise EndpointError(f"{type(e).__name__}: {e}") from e
        if r.status_code in (401, 403):
            raise EndpointError(f"HTTP {r.status_code} (access denied) for {url}",
                                denied=True)
        if r.status_code == 429:
            retry = _num(r.headers.get("retry-after")) or _num(
                r.headers.get("x-rate-limit-retry-after-seconds")) or pacing_delay(r.headers)
            raise EndpointError(f"HTTP 429 (rate limited) for {url}", retry_after_s=retry or None)
        if r.status_code >= 400:
            raise EndpointError(f"HTTP {r.status_code} for {url}")
        return r

    async def _fetch(self, ctx: CollectorContext, ep: Endpoint) -> tuple[float, list[dict]]:
        if ep.kind == "opensky":
            r = await self._get(ctx, ep, f"{ep.url}/states/all")
            data = r.json()
            now_s = float(data.get("time") or time.time())
            mil = self._mil_db
            out = []
            for s in data.get("states") or []:
                if isinstance(s, list) and s and mil is not None and mil.get(str(s[0]).lower()):
                    ac = opensky_to_readsb(s, now_s)
                    if ac is not None:
                        out.append(ac)
            return now_s, out
        r = await self._get(ctx, ep, f"{ep.url}/mil")
        ep.not_before = max(ep.not_before, self._clock() + pacing_delay(r.headers))
        data = r.json()
        return float(data.get("now") or time.time() * 1000) / 1000, data.get("ac") or []

    # --------------------------------------------------------------- endpoint state

    def _due(self, ep: Endpoint, now: float) -> bool:
        if now < ep.not_before:
            return False
        return ep.last_attempt is None or now - ep.last_attempt >= ep.min_interval_s

    def _succeeded(self, ep: Endpoint, now: float) -> None:
        if ep.warned:
            log.info("ADS-B endpoint %s recovered after %d failure(s)", ep.name, ep.failures)
        ep.failures, ep.warned, ep.last_ok = 0, False, now

    def _failed(self, ep: Endpoint, exc: BaseException, now: float) -> str:
        ep.failures += 1
        if isinstance(exc, EndpointError) and exc.denied:
            delay = self.denied_backoff_s
        elif isinstance(exc, EndpointError) and exc.retry_after_s:
            delay = min(self.denied_backoff_s, exc.retry_after_s)
        else:  # 30 s, 60 s, 2 min, ... capped
            delay = min(self.max_backoff_s, 15.0 * 2 ** ep.failures)
        ep.not_before = max(ep.not_before, now + delay)
        msg = f"{ep.name}: {type(exc).__name__}: {exc}"
        if not ep.warned:
            log.warning("ADS-B endpoint failed: %s (backing off %.0f s)", msg, delay)
            ep.warned = True
        else:
            log.debug("ADS-B endpoint still failing (%d): %s", ep.failures, msg)
        return msg

    def status(self) -> dict[str, dict]:
        """Per-endpoint health (for diagnostics)."""
        now = self._clock()
        return {ep.name: {"kind": ep.kind, "fallback": ep.fallback, "failures": ep.failures,
                          "backoff_s": round(max(0.0, ep.not_before - now)),
                          "last_ok_s_ago": None if ep.last_ok is None
                          else round(now - ep.last_ok)} for ep in self.endpoints}

    # ------------------------------------------------------------------- poll

    async def _round(self, ctx: CollectorContext, eps: list[Endpoint], now: float,
                     errors: list[str]) -> list[tuple[str, float, list[dict]]]:
        for ep in eps:
            ep.last_attempt = now
        results = await asyncio.gather(*(self._fetch(ctx, ep) for ep in eps),
                                       return_exceptions=True)
        out = []
        for ep, res in zip(eps, results, strict=True):
            if isinstance(res, asyncio.CancelledError):
                raise res
            if isinstance(res, BaseException):
                errors.append(self._failed(ep, res, now))
                continue
            self._succeeded(ep, now)
            out.append((ep.name, *res))
        return out

    async def poll(self, ctx: CollectorContext) -> Batch:
        if self._mil_db is None:
            self._mil_db = await asyncio.to_thread(
                MilDb.from_csv, ctx.settings.reference_dir / "plane-alert-mil.csv"
            )
        now = self._clock()
        errors: list[str] = []
        primary = [ep for ep in self.endpoints if not ep.fallback and self._due(ep, now)]
        fetched = await self._round(ctx, primary, now, errors)
        if not fetched:  # nothing from the primaries this cycle -> try the fallbacks
            fallback = [ep for ep in self.endpoints if ep.fallback and self._due(ep, now)]
            fetched = await self._round(ctx, fallback, now, errors)
        if not fetched:
            waiting = {ep.name: math.ceil(ep.not_before - now) for ep in self.endpoints
                       if ep.not_before > now}
            detail = "; ".join(errors) or f"all endpoints backing off {waiting}"
            raise RuntimeError("all ADS-B endpoints failed: " + detail)
        best: dict[str, tuple[float, dict, float, str]] = {}
        skipped = 0
        for api, now_s, aircraft in fetched:
            for ac in aircraft:
                try:
                    hex_code = (ac.get("hex") or "").strip().lower()
                    age = float(ac.get("seen_pos", ac.get("seen", 1e9)) or 0)
                except (ValueError, TypeError, KeyError, AttributeError):
                    skipped += 1
                    continue
                if hex_code and (hex_code not in best or age < best[hex_code][0]):
                    best[hex_code] = (age, ac, now_s, api)
        batch = Batch(source=self.name)
        for _, ac, now_s, api in best.values():
            try:
                parsed = parse_aircraft(ac, now_s, self._mil_db, f"adsb:{api}")
            except (ValueError, TypeError, KeyError):
                skipped += 1
                continue
            if parsed is None:
                continue
            pos, track = parsed
            batch.tracks.append(track)
            if pos is not None:
                batch.positions.append(pos)
        if skipped:
            log.warning("ADS-B: skipped %d malformed aircraft record(s)", skipped)
        return batch
