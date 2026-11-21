"""Military movements endpoints: corridors, legs, surges, naval (cached, msgspec-encoded)."""

import asyncio
import time
from collections.abc import Callable

import msgspec
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

from osint.core.geo import utcnow
from osint.movements.config import MovementsConfig
from osint.movements.roles import ROLES
from osint.movements.service import MovementsService

_ENC = msgspec.json.Encoder()
_STRIP = ("t_start_us", "t_end_us")


class _Cache:
    """key -> (expires_monotonic, body); one computation per key at a time."""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._data: dict[tuple, tuple[float, bytes]] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}

    async def get(self, key: tuple, build: Callable[[], dict]) -> bytes:
        hit = self._data.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._data.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            body = _ENC.encode(await asyncio.to_thread(build))
            if len(self._data) > 64:
                self._data.clear()
            self._data[key] = (time.monotonic() + self.ttl, body)
            return body


def _role(role: str | None) -> str | None:
    if role and role not in ROLES:
        raise HTTPException(422, "unknown role; one of " + ", ".join(ROLES))
    return role or None


def _env(layer: str, hours: int, items: list, **extra) -> dict:
    return {"layer": layer, "generated_at": utcnow().isoformat(), "hours": hours,
            "count": len(items), "items": items, **extra}


def _slim(legs: list[dict]) -> list[dict]:
    return [{k: v for k, v in lg.items() if k not in _STRIP} for lg in legs]


def mount_movements(app: FastAPI, runtime=None, service: MovementsService | None = None) -> None:
    if service is None:
        from osint.reference.loader import get_reference

        cfg = MovementsConfig.load(runtime.settings.config_dir / "movements.yaml")
        service = MovementsService(runtime.lake, runtime.store, lambda: get_reference().airports, cfg)
    svc = service
    cache = _Cache(svc.cfg.cache_s)
    app.state.movements = svc

    def _json(body: bytes) -> Response:
        return Response(body, media_type="application/json")

    @app.get("/api/movements/corridors")
    async def corridors(hours: int = Query(168, ge=1, le=336), role: str | None = None) -> Response:
        r = _role(role)
        return _json(await cache.get(("corridors", hours, r), lambda: _env(
            "movements.corridors", hours, svc.corridors(hours, r))))

    @app.get("/api/movements/legs")
    async def legs(hours: int = Query(24, ge=1, le=168), role: str | None = None) -> Response:
        r = _role(role)
        return _json(await cache.get(("legs", hours, r), lambda: _env(
            "movements.legs", hours, _slim(svc.legs(hours, r)))))

    @app.get("/api/movements/surges")
    async def surges(hours: int = Query(24, ge=6, le=168)) -> Response:
        def build() -> dict:
            res = svc.surges(hours)
            return _env("movements.surges", hours, res["items"],
                        baseline_insufficient=res["baseline_insufficient"],
                        history_days=res["history_days"], z_min=svc.cfg.surges.z_min,
                        h3_res=svc.cfg.surges.h3_res, bucket_hours=svc.cfg.surges.bucket_hours)
        return _json(await cache.get(("surges", hours), build))

    @app.get("/api/movements/naval")
    async def naval(hours: int = Query(24, ge=1, le=168)) -> Response:
        return _json(await cache.get(("naval", hours), lambda: _env(
            "movements.naval", hours, svc.naval(hours))))
