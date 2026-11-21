"""Webcam catalogue: fetch/parse per source, JSON cache, bbox/near queries, snapshot proxy with TTL cache."""

import asyncio
import json
import logging
import math
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from osint.config import ROOT
from osint.reference.geometry import in_bbox
from osint.webcams.models import Webcam
from osint.webcams.settings import WebcamSettings, load_config, windy_key
from osint.webcams.sources import REGISTRY

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^[a-z0-9]+:[A-Za-z0-9_.\-]{1,80}$")
MAX_IMAGE_BYTES = 5 * 1024 * 1024
HEADERS = {"User-Agent": "osint-poc/0.1 (public webcam catalogue)"}


def default_cache_path() -> Path:
    return ROOT / "data" / "reference" / "webcams.json"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(a))


class SnapshotError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


class Catalogue:
    def __init__(self, cache_path: Path | None = None, config: dict[str, Any] | None = None,
                 client_factory=None, settings: WebcamSettings | None = None):
        self.cache_path = cache_path or default_cache_path()
        self.cfg = config if config is not None else load_config()
        self.refresh_s = float(self.cfg.get("refresh_hours", 6)) * 3600
        self.snapshot_ttl = float(self.cfg.get("snapshot_ttl_s", 30))
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=30, headers=HEADERS))
        self.settings = settings
        self.items: dict[str, Webcam] = {}
        self.meta: dict[str, dict] = {}
        self.fetched_at = 0.0
        self.loaded = False
        self._lock = asyncio.Lock()
        self._bg: asyncio.Task | None = None
        self._snaps: dict[str, tuple[float, bytes, str]] = {}

    # ---- persistence -------------------------------------------------
    def load_cache(self) -> None:
        self.loaded = True
        try:
            d = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.items = {w["id"]: Webcam(**w) for w in d.get("items", [])}
            self.meta = d.get("sources", {})
            self.fetched_at = float(d.get("fetched_at", 0))
        except (OSError, ValueError, TypeError, KeyError):
            self.items, self.meta, self.fetched_at = {}, {}, 0.0

    def save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fetched_at": self.fetched_at, "sources": self.meta,
                                       "items": [w.model_dump() for w in self.items.values()]}), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError:
            log.warning("could not write %s", self.cache_path)

    # ---- refresh -----------------------------------------------------
    def _enabled(self, sid: str) -> bool:
        return bool(((self.cfg.get("sources") or {}).get(sid) or {}).get("enabled", True))

    async def _get(self, client: httpx.AsyncClient, url: str, headers: dict, params=None) -> bytes:
        r = await client.get(url, headers=headers, params=params)
        r.raise_for_status()
        return r.content

    async def _fetch_source(self, client, sid: str, now: str) -> tuple[list[Webcam], str | None]:
        src, decode, parse = REGISTRY[sid]
        headers = dict(src.headers)
        urls: list[tuple[str, dict | None]] = [(u, None) for u in src.urls]
        if sid == "windy":
            key = windy_key(self.cfg, self.settings)
            headers["x-windy-api-key"] = key
            pages = int(self.cfg.get("windy_max_pages", 20))
            urls = [(src.urls[0], {"limit": 50, "offset": i * 50, "include": "location,images,player,urls"})
                    for i in range(pages)]
        out: list[Webcam] = []
        errors: list[str] = []
        for url, params in urls:
            try:
                out.extend(parse(decode(await self._get(client, url, headers, params)), now))
            except (httpx.HTTPError, ValueError) as e:
                errors.append(f"{url.rsplit('/', 1)[-1]}: {type(e).__name__} {str(e)[:80]}")
                if sid == "windy":
                    break
        return out, ("; ".join(errors[:3]) + (f" (+{len(errors) - 3} more)" if len(errors) > 3 else "")) if errors else None

    async def refresh(self) -> dict[str, dict]:
        async with self._lock:
            if not self.loaded:
                await asyncio.to_thread(self.load_cache)
            now = datetime.now(UTC).isoformat(timespec="seconds")
            async with self.client_factory() as client:
                sids = [s for s in REGISTRY if self._enabled(s)]
                runnable = [s for s in sids if s != "windy" or windy_key(self.cfg, self.settings)]
                results = await asyncio.gather(*(self._fetch_source(client, s, now) for s in runnable))
            new = {k: w for k, w in self.items.items() if k.split(":", 1)[0] not in sids}
            meta: dict[str, dict] = {}
            for sid in REGISTRY:
                src = REGISTRY[sid][0]
                m = {"id": sid, "name": src.name, "licence": src.licence, "attribution": src.attribution,
                     "url": src.url, "commercial_ok": src.commercial_ok, "enabled": sid in sids,
                     "count": 0, "last_refresh": None, "error": None}
                if sid not in sids:
                    m["error"] = "disabled in config"
                elif sid not in runnable:
                    m["error"] = "skipped: WINDY_WEBCAMS_KEY not set"
                else:
                    items, err = results[runnable.index(sid)]
                    if items:
                        for w in items:
                            new[w.id] = w
                        m.update(count=len(items), last_refresh=now, error=err)
                    else:  # failed entirely: keep previously cached items for this source
                        kept = [w for w in self.items.values() if w.source == sid]
                        for w in kept:
                            new[w.id] = w
                        old = self.meta.get(sid, {})
                        m.update(count=len(kept), last_refresh=old.get("last_refresh"), error=err or "no cameras returned")
                meta[sid] = m
            self.items, self.meta, self.fetched_at = new, meta, time.time()
            await asyncio.to_thread(self.save_cache)
            return meta

    def stale(self) -> bool:
        return time.time() - self.fetched_at > self.refresh_s

    async def ensure_fresh(self) -> None:
        if not self.loaded:
            await asyncio.to_thread(self.load_cache)
        if not self.items:
            await self.refresh()
        elif self.stale() and (self._bg is None or self._bg.done()):
            self._bg = asyncio.create_task(self._bg_refresh())

    async def _bg_refresh(self) -> None:
        try:
            await self.refresh()
        except Exception:  # noqa: BLE001 - background task must never crash the app
            log.exception("webcam background refresh failed")

    # ---- queries -----------------------------------------------------
    def query(self, bbox=None, sources: set[str] | None = None, limit: int = 5000) -> list[Webcam]:
        out = []
        for w in self.items.values():
            if sources and w.source not in sources:
                continue
            if in_bbox(w.lat, w.lon, bbox):
                out.append(w)
                if len(out) >= limit:
                    break
        return out

    def near(self, lat: float, lon: float, radius_km: float = 25, limit: int = 10) -> list[dict]:
        hits = []
        for w in self.items.values():
            d = haversine_km(lat, lon, w.lat, w.lon)
            if d <= radius_km:
                hits.append((d, w))
        hits.sort(key=lambda t: t[0])
        return [{**w.slim(), "distance_km": round(d, 2), "refresh_s": w.refresh_s} for d, w in hits[:limit]]

    def sources_info(self) -> list[dict]:
        out = []
        for sid, (src, _, _) in REGISTRY.items():
            m = self.meta.get(sid) or {"id": sid, "name": src.name, "licence": src.licence,
                                       "attribution": src.attribution, "url": src.url,
                                       "commercial_ok": src.commercial_ok, "enabled": self._enabled(sid),
                                       "count": 0, "last_refresh": None, "error": None}
            out.append(m)
        return out

    # ---- snapshot proxy ----------------------------------------------
    async def snapshot(self, cam_id: str) -> tuple[bytes, str, Webcam]:
        if not ID_RE.match(cam_id):
            raise SnapshotError(422, "invalid camera id")
        await self.ensure_fresh()
        cam = self.items.get(cam_id)
        if cam is None:
            raise SnapshotError(404, "unknown camera")
        hit = self._snaps.get(cam_id)
        if hit and time.monotonic() - hit[0] < self.snapshot_ttl:
            return hit[1], hit[2], cam
        headers = dict(REGISTRY[cam.source][0].headers)
        try:
            async with self.client_factory() as client:
                async with client.stream("GET", cam.image_url, headers=headers) as r:
                    if r.status_code != 200:
                        raise SnapshotError(502, f"upstream status {r.status_code}")
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if not ctype.startswith("image/"):
                        raise SnapshotError(502, "upstream is not an image")
                    if int(r.headers.get("content-length") or 0) > MAX_IMAGE_BYTES:
                        raise SnapshotError(502, "image too large")
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) > MAX_IMAGE_BYTES:
                            raise SnapshotError(502, "image too large")
        except httpx.HTTPError as e:
            raise SnapshotError(502, f"upstream error: {type(e).__name__}") from None
        self._snaps[cam_id] = (time.monotonic(), bytes(buf), ctype)
        if len(self._snaps) > 500:
            for k in sorted(self._snaps, key=lambda k: self._snaps[k][0])[:100]:
                del self._snaps[k]
        return bytes(buf), ctype, cam


_default: Catalogue | None = None


def get_catalogue() -> Catalogue:
    global _default
    if _default is None:
        _default = Catalogue()
    return _default
