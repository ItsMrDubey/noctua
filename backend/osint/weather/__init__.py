"""Current cloud cover / wind on a view-sized grid (Open-Meteo), cached per grid node.

The frontend sends its view bbox; we snap it to a grid whose step scales with the view
(0.25 deg close in .. 2 deg zoomed out, at most MAX_POINTS nodes), fetch only the nodes not
already cached in one multi-location request, and return low/mid/high cloud cover (%) plus
10 m wind. Nodes are cached for CACHE_TTL_S (the model updates every 15 min), so panning
over the same area costs nothing.

Open-Meteo's free API is for non-commercial use (see the licence register).
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass

import httpx

API = "https://api.open-meteo.com/v1/forecast"
FIELDS = ("cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
          "wind_speed_10m", "wind_direction_10m")
STEPS = (0.25, 0.5, 1.0, 2.0)
MAX_POINTS = 64
CACHE_TTL_S = 20 * 60
MAX_CACHE = 20_000
USER_AGENT = "osint-poc/0.1"


@dataclass
class Node:
    lat: float
    lon: float
    low: float
    mid: float
    high: float
    wind_speed: float | None
    wind_dir: float | None
    at: float

    def out(self) -> dict:
        return {"lat": self.lat, "lon": self.lon, "low": self.low, "mid": self.mid,
                "high": self.high, "wind_speed": self.wind_speed, "wind_dir": self.wind_dir}


def grid(bbox: tuple[float, float, float, float]) -> tuple[float, list[tuple[float, float]]]:
    """Grid step and nodes (lat, lon) covering ``bbox`` = (west, south, east, north)."""
    w, s, e, n = bbox
    if e < w:  # antimeridian: cover the eastern part only (rare for close-in views)
        e = 180.0
    s, n = max(s, -85.0), min(n, 85.0)
    span = max(e - w, n - s, 1e-6)
    step = next((st for st in STEPS if span / st <= math.sqrt(MAX_POINTS) - 1), STEPS[-1])
    lats = [round(v * step, 4) for v in range(math.floor(s / step), math.ceil(n / step) + 1)]
    lons = [round(v * step, 4) for v in range(math.floor(w / step), math.ceil(e / step) + 1)]
    nodes = [(la, lo) for la in lats for lo in lons if -180 <= lo <= 180]
    if len(nodes) > MAX_POINTS:  # thin evenly
        k = math.ceil(len(nodes) / MAX_POINTS)
        nodes = nodes[::k]
    return step, nodes


class CloudService:
    def __init__(self, client: httpx.AsyncClient | None = None, clock=time.monotonic) -> None:
        self._client = client
        self.clock = clock
        self.cache: dict[tuple[float, float], Node] = {}
        self._lock = asyncio.Lock()
        self.requests = 0
        self.last_error: str | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=12.0, headers={"User-Agent": USER_AGENT})
        return self._client

    async def _fetch(self, nodes: list[tuple[float, float]]) -> None:
        params = {"latitude": ",".join(f"{la:g}" for la, _ in nodes),
                  "longitude": ",".join(f"{lo:g}" for _, lo in nodes),
                  "current": ",".join(FIELDS), "wind_speed_unit": "kn"}
        self.requests += 1
        r = await self._http().get(API, params=params)
        r.raise_for_status()
        data = r.json()
        rows = data if isinstance(data, list) else [data]
        now = self.clock()
        for (la, lo), row in zip(nodes, rows):
            cur = row.get("current") or {}
            self.cache[(la, lo)] = Node(
                la, lo, float(cur.get("cloud_cover_low") or 0), float(cur.get("cloud_cover_mid") or 0),
                float(cur.get("cloud_cover_high") or 0), cur.get("wind_speed_10m"),
                cur.get("wind_direction_10m"), now)
        if len(self.cache) > MAX_CACHE:  # drop the oldest half
            for k, _ in sorted(self.cache.items(), key=lambda kv: kv[1].at)[: len(self.cache) // 2]:
                del self.cache[k]

    async def clouds(self, bbox: tuple[float, float, float, float]) -> dict:
        step, nodes = grid(bbox)
        now = self.clock()
        missing = [k for k in nodes
                   if k not in self.cache or now - self.cache[k].at > CACHE_TTL_S]
        stale = False
        if missing:
            async with self._lock:
                missing = [k for k in missing
                           if k not in self.cache or self.clock() - self.cache[k].at > CACHE_TTL_S]
                if missing:
                    try:
                        await self._fetch(missing)
                        self.last_error = None
                    except (httpx.HTTPError, ValueError) as exc:
                        self.last_error = f"{type(exc).__name__}: {exc}"
                        stale = True
        items = [self.cache[k].out() for k in nodes if k in self.cache]
        return {"step": step, "count": len(items), "items": items, "stale": stale,
                "source": "Open-Meteo", "error": self.last_error if stale else None}
