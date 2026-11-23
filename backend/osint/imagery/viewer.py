"""Imagery viewer service: scene catalogue + on-demand renders with an on-disk LRU cache.

Rendered windows are written to ``data_dir/imagery/viewer/<key>.<ext>`` with a ``.json``
sidecar (bounds, m/px, stretch, attribution...). The key hashes everything that changes the
pixels, so a repeated request (thumbnail strip, compare, drape) is a file read. The cache is
trimmed oldest-first past ``max_cache_mb``.
"""

import hashlib
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

from osint.imagery.catalog import Catalogue, attribution, parse_dt
from osint.imagery.render import PRESETS, Renderer, RenderError, encode, make_grid
from osint.imagery.tiff import Unsupported

log = logging.getLogger(__name__)
USER_AGENT = "osint-poc/0.1 (imagery viewer)"
RENDER_VERSION = 2  # bump when the output (pixels or metadata) of a request would change


def make_client(connect_s: float = 10.0, read_s: float = 60.0) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True,
                        timeout=httpx.Timeout(read_s, connect=connect_s))


class ViewerService:
    def __init__(self, data_dir: Path, max_cache_mb: float = 512.0, client_factory=None) -> None:
        self.dir = data_dir / "imagery" / "viewer"
        self.client_factory = client_factory or make_client
        self.catalogue = Catalogue(self.client_factory, cache_dir=self.dir)
        self.renderer = Renderer()
        self.max_bytes = int(max_cache_mb * 1024 * 1024)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._writes = 0

    # ---- catalogue
    def scenes(self, lat: float, lon: float, start: datetime, end: datetime,
               sources: tuple[str, ...]) -> dict:
        return self.catalogue.search(lat, lon, start, end, sources)

    # ---- render
    def render_key(self, scene: dict, other: dict | None, preset: str, lat: float, lon: float,
                   km: float, size: str, fmt: str) -> str:
        ident = [RENDER_VERSION, scene["key"], other["key"] if other else None, preset,
                 round(lat, 5), round(lon, 5), round(km, 3), size, fmt]
        return hashlib.sha1(json.dumps(ident).encode()).hexdigest()[:24]

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def cached(self, name: str) -> Path | None:
        """Path of a cached image by file name (``<key>.<ext>``), or None."""
        p = self.dir / name
        return p if p.is_file() else None

    def render(self, key: str, preset: str, lat: float, lon: float, km: float,
               size: str = "full", key2: str | None = None, fmt: str = "webp") -> dict:
        """Render (or fetch from cache) and return metadata incl. ``file`` (cache name)."""
        scene = self.catalogue.get(key)
        if scene is None:
            raise RenderError("unknown scene (search the site first)")
        other = self.catalogue.get(key2) if key2 else None
        if key2 and other is None:
            raise RenderError("unknown second scene (search the site first)")
        if scene["renderer"] == "tiles":
            raise RenderError("Wayback releases are shown as Esri tiles, not rendered")
        if preset not in PRESETS:
            raise RenderError(f"unknown preset {preset!r}")
        fmt = "png" if fmt == "png" else "webp"
        rkey = self.render_key(scene, other, preset, lat, lon, km, size, fmt)
        ext = "png" if fmt == "png" else "webp"
        img, side = self.dir / f"{rkey}.{ext}", self.dir / f"{rkey}.json"
        with self._lock_for(rkey):
            if img.is_file() and side.is_file():
                try:
                    meta = json.loads(side.read_text(encoding="utf-8"))
                    img.touch()
                    return {**meta, "cached": True, "took_ms": 0}
                except (OSError, ValueError):
                    pass
            t0 = time.perf_counter()
            grid = make_grid(lat, lon, km, scene["gsd_m"] or 10.0, size)
            try:
                with self.client_factory() as client:
                    rgba, meta = self.renderer.render(client, scene, preset, grid, other)
            except (Unsupported, ValueError, KeyError, OSError) as exc:
                raise RenderError(f"{type(exc).__name__}: {exc}") from exc
            data, media = encode(rgba, fmt)
            self.dir.mkdir(parents=True, exist_ok=True)
            valid = float((rgba[..., 3] > 0).mean()) * 100
            meta = {**meta, "file": img.name, "media_type": media,
                    "bounds": {"west": grid.west, "south": grid.south, "east": grid.east,
                               "north": grid.north},
                    "px": [grid.width, grid.height], "m_per_px": round(grid.m_per_px(), 3),
                    "native_m": scene["gsd_m"], "km": km, "size": size,
                    "valid_pct": round(valid, 1),
                    "scene": {k: scene[k] for k in ("key", "source", "name", "platform", "sensor",
                                                    "acquired_at", "cloud_pct", "gsd_m")},
                    "scene2": ({k: other[k] for k in ("key", "acquired_at", "platform")}
                               if other else None),
                    "attribution": attribution(scene["source"], scene["acquired_at"]),
                    "render_ms": round((time.perf_counter() - t0) * 1000)}
            if valid == 0:
                raise RenderError("no valid pixels over the site in this scene")
            img.write_bytes(data)
            side.write_text(json.dumps(meta), encoding="utf-8")
        self._writes += 1
        if self._writes % 20 == 1:
            self.trim()
        return {**meta, "cached": False, "took_ms": meta["render_ms"]}

    def trim(self) -> None:
        """Delete the least recently used renders past the size budget."""
        try:
            files = [(p.stat().st_mtime, p.stat().st_size, p) for p in self.dir.glob("*.*")]
        except OSError:
            return
        total = sum(f[1] for f in files)
        for _, size, p in sorted(files):
            if total <= self.max_bytes:
                break
            try:
                p.unlink()
                total -= size
            except OSError:
                pass


def parse_when(raw: str | None, default: datetime) -> datetime:
    return parse_dt(raw) if raw else default


_SERVICE_LOCK = threading.Lock()


def get_service(runtime) -> ViewerService:
    svc = getattr(runtime, "imagery_viewer", None)
    if svc is None:
        with _SERVICE_LOCK:
            svc = getattr(runtime, "imagery_viewer", None)
            if svc is None:
                svc = ViewerService(runtime.settings.data_dir)
                runtime.imagery_viewer = svc
    return svc
