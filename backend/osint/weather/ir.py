"""Seamless global infrared cloud imagery: which geostationary frame to show where, and when.

Five geostationary imagers cover the globe between them. Each is shown only inside its own
longitude sector (boundaries at the midpoints between neighbouring sub-satellite longitudes,
latitude cut at +/-LAT_LIMIT) so the disks never overlap or double-darken:

    GOES-West 137.2W | GOES-East 75.2W | Meteosat (MTG-I) 0 | Meteosat IODC 45.5E | Himawari 140.7E

NASA GIBS (GOES, Himawari) publishes 10-minute frames 20-60 min after the scan and has gaps,
so a fixed "now - 30 min" slot often 404s; a brand-new slot also reaches its tile servers
piecemeal. We probe each layer's 10-minute slots from now backwards (a level-0 and a level-2
tile per probe) and use the newest complete slot (see IRFrames._gibs_latest). EUMETSAT's View
Service (Meteosat) reports its latest frame in a per-layer WMS GetCapabilities (TIME default).
Results are cached for CACHE_S so all clients share one probe round.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best"
EUMETSAT = "https://view.eumetsat.int/geoserver"
LAT_LIMIT = 70.0
CACHE_S = 240
PROBE_MIN_LAG_MIN = 10   # nothing newer than this is ever published
PROBE_MAX_STEPS = 24     # 10-min slots tried before giving up (4 h)
USER_AGENT = "osint-poc/0.1"


@dataclass(frozen=True)
class GeoSource:
    id: str
    label: str          # short label for the status line
    name: str           # satellite / instrument
    lon: float          # sub-satellite longitude
    kind: str           # "gibs" (WMTS, colour-mapped BT) | "wms" (EUMETSAT, grey BT)
    layer: str
    step_min: int       # repeat cycle of the frames
    max_level: int      # deepest useful level of 256 px web-mercator tiles (~source resolution;
                        # beyond it Cesium's linear filtering upsamples smoothly)
    decode: str         # how the frontend turns pixels into brightness temperature
    band: str
    fallback: GeoSource | None = None


MSG_0 = GeoSource("msg", "MSG 0°", "Meteosat SEVIRI 0°", 0.0, "wms", "msg_fes:ir108", 15, 6, "msg", "IR 10.8 µm")
SOURCES: tuple[GeoSource, ...] = (
    GeoSource("goes_west", "G-W", "GOES-West ABI", -137.2, "gibs", "GOES-West_ABI_Band13_Clean_Infrared", 10, 6, "gibs", "IR 10.3 µm"),
    GeoSource("goes_east", "G-E", "GOES-East ABI", -75.2, "gibs", "GOES-East_ABI_Band13_Clean_Infrared", 10, 6, "gibs", "IR 10.3 µm"),
    GeoSource("mtg", "MTG", "Meteosat MTG-I FCI 0°", 0.0, "wms", "mtg_fd:ir105_hrfi", 10, 7, "mtg", "IR 10.5 µm", fallback=MSG_0),
    GeoSource("iodc", "IODC", "Meteosat SEVIRI IODC 45.5°E", 45.5, "wms", "msg_iodc:ir108", 15, 6, "msg", "IR 10.8 µm"),
    GeoSource("himawari", "HIM", "Himawari AHI", 140.7, "gibs", "Himawari_AHI_Band13_Clean_Infrared", 10, 6, "gibs", "IR 10.4 µm"),
)


def _wrap(lon: float) -> float:
    """Longitude into [-180, 180)."""
    return (lon + 180.0) % 360.0 - 180.0


def sectors(lons: list[float]) -> list[list[tuple[float, float]]]:
    """Longitude sector(s) of each sub-satellite longitude in ``lons``.

    Each satellite owns the longitudes closer to it than to any other (boundaries at the
    midpoints, going round the globe). A sector that crosses the antimeridian is split into
    two (west, east) pieces so every piece has west < east. Returned in the input order.
    """
    n = len(lons)
    if n == 0:
        return []
    if n == 1:
        return [[(-180.0, 180.0)]]
    order = sorted(range(n), key=lambda i: _wrap(lons[i]))
    ws = [_wrap(lons[i]) for i in order]
    out: list[list[tuple[float, float]]] = [[] for _ in range(n)]
    for k, i in enumerate(order):
        prev = ws[k - 1] - (360.0 if k == 0 else 0.0)
        nxt = ws[(k + 1) % n] + (360.0 if k == n - 1 else 0.0)
        west, east = (prev + ws[k]) / 2.0, (ws[k] + nxt) / 2.0
        pieces: list[tuple[float, float]] = []
        if west < -180.0:
            pieces += [(west + 360.0, 180.0), (-180.0, east)]
        elif east > 180.0:
            pieces += [(west, 180.0), (-180.0, east - 360.0)]
        else:
            pieces.append((west, east))
        out[i] = [(round(w, 3), round(e, 3)) for w, e in pieces if e - w > 1e-9]
    return out


def floor_slot(t: datetime, step_min: int) -> datetime:
    """``t`` rounded down to a ``step_min`` slot (UTC, minutes since midnight)."""
    t = t.astimezone(UTC).replace(second=0, microsecond=0)
    m = t.hour * 60 + t.minute
    return t.replace(hour=0, minute=0) + timedelta(minutes=m - m % step_min)


def candidate_slots(now: datetime, step_min: int = 10, min_lag_min: int = PROBE_MIN_LAG_MIN,
                    steps: int = PROBE_MAX_STEPS) -> list[datetime]:
    """Slots to probe, newest first: from ``now - min_lag`` back ``steps`` slots."""
    start = floor_slot(now - timedelta(minutes=min_lag_min), step_min)
    return [start - timedelta(minutes=step_min * k) for k in range(steps)]


def iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def gibs_tile(layer: str, t: datetime | str, z: int = 0, y: int = 0, x: int = 0) -> str:
    ts = t if isinstance(t, str) else iso(t)
    return f"{GIBS}/{layer}/default/{ts}/GoogleMapsCompatible_Level6/{z}/{y}/{x}.png"


def tile_xy(lat: float, lon: float, z: int) -> tuple[int, int]:
    """Web-mercator tile (x, y) containing lat/lon at zoom ``z``."""
    n = 1 << z
    x = int((_wrap(lon) + 180.0) / 360.0 * n)
    la = math.radians(max(-85.0, min(85.0, lat)))
    y = int((1.0 - math.asinh(math.tan(la)) / math.pi) / 2.0 * n)
    return min(x, n - 1), min(y, n - 1)


_DIM = re.compile(r'<Dimension[^>]*name="time"[^>]*default="([^"]+)"', re.IGNORECASE)


def parse_wms_default_time(xml: str) -> datetime | None:
    """TIME default (latest frame) from a GeoServer WMS capabilities document."""
    m = _DIM.search(xml)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1)).astimezone(UTC)
    except ValueError:
        return None


async def probe_latest(exists, slots: list[datetime]) -> tuple[datetime | None, int]:
    """Newest slot for which ``await exists(slot)`` is true; (slot, probes used).

    ``exists`` returns True (frame there), False (not there: step back) or raises (upstream
    trouble: stop, so an outage does not turn into a burst of requests)."""
    for k, s in enumerate(slots):
        if await exists(s):
            return s, k + 1
    return None, len(slots)


@dataclass
class Frame:
    source: GeoSource
    time: datetime | None = None
    error: str | None = None
    probes: int = 0
    used: GeoSource | None = None   # the source actually served (fallback when the primary failed)
    checked: float = 0.0

    def out(self, sector: list[tuple[float, float]], now: datetime) -> dict:
        s = self.used or self.source
        return {
            "id": self.source.id, "label": s.label, "name": s.name, "lon": s.lon, "kind": s.kind,
            "layer": s.layer, "decode": s.decode, "band": s.band, "max_level": s.max_level,
            "step_min": s.step_min, "sector": [list(p) for p in sector], "lat_limit": LAT_LIMIT,
            "time": iso(self.time) if self.time else None,
            "age_min": round((now - self.time).total_seconds() / 60) if self.time else None,
            "fallback": self.used is not None and self.used is not self.source,
            "error": self.error,
        }


@dataclass
class IRFrames:
    client: httpx.AsyncClient | None = None
    clock: object = time.monotonic
    now: object = field(default=lambda: datetime.now(UTC))
    cache: dict | None = None
    at: float = -1e9
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _http(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=10.0, headers={"User-Agent": USER_AGENT})
        return self.client

    async def _gibs_latest(self, src: GeoSource) -> tuple[datetime | None, int]:
        """Newest *settled* GIBS frame. A fresh slot reaches GIBS's tile servers piecemeal (for
        a few minutes some levels / tiles of it still 404), so a slot counts as present only
        when a level-0 tile and a level-2 tile under the satellite both exist, and the slot
        before it is used when that one is complete too (a frame 10 min older, but whole)."""
        http = self._http()
        z2 = tile_xy(0.0, src.lon, 2)

        async def tile_ok(slot: datetime, z: int, x: int, y: int) -> bool:
            r = await http.get(gibs_tile(src.layer, slot, z, y, x))
            if r.status_code == 200:
                return True
            if r.status_code in (400, 404):
                return False
            raise httpx.HTTPError(f"GIBS {r.status_code}")

        async def exists(slot: datetime) -> bool:
            return await tile_ok(slot, 0, 0, 0) and await tile_ok(slot, 2, *z2)

        newest, n = await probe_latest(exists, candidate_slots(self.now(), src.step_min))
        if newest is None:
            return None, n
        prev = newest - timedelta(minutes=src.step_min)
        return (prev if await exists(prev) else newest), n + 1

    async def _wms_latest(self, src: GeoSource) -> datetime | None:
        ws, name = src.layer.split(":", 1)
        r = await self._http().get(f"{EUMETSAT}/{ws}/{name}/ows",
                                   params={"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"})
        r.raise_for_status()
        t = parse_wms_default_time(r.text)
        if t is None:
            raise ValueError("no TIME default in capabilities")
        if t > self.now() + timedelta(minutes=5):
            raise ValueError(f"TIME default in the future: {iso(t)}")
        return t

    async def _resolve(self, src: GeoSource) -> Frame:
        f = Frame(src, checked=self.clock())
        for cand in (src, src.fallback):
            if cand is None:
                break
            try:
                if cand.kind == "gibs":
                    t, f.probes = await self._gibs_latest(cand)
                    if t is None:
                        raise ValueError(f"no frame in the last {PROBE_MAX_STEPS * cand.step_min // 60} h")
                else:
                    t = await self._wms_latest(cand)
                f.time, f.used, f.error = t, cand, None
                return f
            except (httpx.HTTPError, ValueError) as exc:
                f.error = f"{cand.label}: {type(exc).__name__}: {exc}"[:200]
        return f

    async def frames(self, force: bool = False) -> dict:
        if not force and self.cache is not None and self.clock() - self.at < CACHE_S:
            return self.cache
        async with self._lock:
            if not force and self.cache is not None and self.clock() - self.at < CACHE_S:
                return self.cache
            got = await asyncio.gather(*(self._resolve(s) for s in SOURCES))
            prev = {d["id"]: d for d in (self.cache or {}).get("sources", [])}
            now = self.now()
            secs = sectors([s.lon for s in SOURCES])
            out = []
            for f, sec in zip(got, secs):
                d = f.out(sec, now)
                # upstream hiccup: keep showing the last good frame rather than dropping a sector
                if d["time"] is None and prev.get(d["id"], {}).get("time"):
                    d = {**prev[d["id"]], "error": d["error"], "stale": True}
                out.append(d)
            self.cache = {"sources": out, "generated": iso(now), "lat_limit": LAT_LIMIT,
                          "refresh_s": CACHE_S}
            self.at = self.clock()
            return self.cache


def sector_of(lon: float, data: dict) -> str | None:
    """Id of the source whose sector contains ``lon`` (for tests / debugging)."""
    lon = _wrap(lon)
    for s in data["sources"]:
        if any(w <= lon < e or (e == 180.0 and math.isclose(lon, 180.0)) for w, e in s["sector"]):
            return s["id"]
    return None
