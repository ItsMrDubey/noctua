"""Asset profiles (osint.profiles): who / what a tracked aircraft or vessel is, why it is listed,
who owns it and what it has been doing, every value with its source.

GET /api/profile/aircraft/{key}   key: ICAO hex ("43c38c", "air:43c38c") or "reg:RA-85041"
GET /api/profile/vessel/{key}     key: MMSI ("538012620", "sea:538012620") or "imo:9336414"
GET /api/profile/sources          lookup sources (licence / terms / TTLs)

Third-party lookups (hexdb.io, adsbdb.com, Planespotters, Wikimedia Commons) are answered
from the server-side cache only; a miss starts a background fetch and the source reports
``pending`` (the client polls again while ``pending`` is true). Local context (tracks, lake
history, sanctions graph, signals) is gathered in a worker thread and memoised for
``LOCAL_TTL_S`` so polling stays cheap.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic_settings import BaseSettings, SettingsConfigDict

from osint.config import ROOT
from osint.profiles import behaviour as bh
from osint.profiles import build
from osint.profiles.lookups import SPECS, Lookups
from osint.reference.loader import default_dir

log = logging.getLogger(__name__)

LOCAL_TTL_S = 20.0


class _Contact(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8",
                                      extra="ignore")
    OSINT_CONTACT: str = ""


class _Memo:
    def __init__(self, ttl_s: float) -> None:
        self.ttl = ttl_s
        self._d: dict[str, tuple[float, dict]] = {}

    def get(self, key: str) -> dict | None:
        hit = self._d.get(key)
        return hit[1] if hit and time.monotonic() - hit[0] < self.ttl else None

    def put(self, key: str, val: dict) -> None:
        if len(self._d) > 256:
            now = time.monotonic()
            self._d = {k: v for k, v in self._d.items() if now - v[0] < self.ttl}
        self._d[key] = (time.monotonic(), val)


def _warm(ctx: build.Context) -> None:
    try:
        bh.country_index()
        ctx.ports()
        ctx.frontline_lines()
    except Exception as exc:  # noqa: BLE001 — warm-up only
        log.info("profile warm-up failed: %s", exc)


def mount_profiles(app: FastAPI, runtime=None, *, ref_dir: Path | None = None,
                   data_dir: Path | None = None, lookups: Lookups | None = None,
                   plane_alert_csv: Path | None = None) -> None:
    settings = getattr(runtime, "settings", None)
    data_dir = data_dir or (settings.data_dir if settings is not None else ROOT / "data")
    ref_dir = ref_dir or default_dir()
    pa_csv = plane_alert_csv or ((settings.reference_dir if settings is not None else ROOT / "reference")
                                 / "plane-alert-mil.csv")
    ctx = build.Context(runtime, ref_dir, data_dir, pa_csv)
    state: dict = {"lookups": lookups}
    memo = _Memo(LOCAL_TTL_S)
    if runtime is not None:  # load the country outlines / ports / front line off the request path
        threading.Thread(target=_warm, args=(ctx,), name="profile-warmup", daemon=True).start()

    def lk() -> Lookups:
        if state["lookups"] is None:
            contact = os.environ.get("OSINT_CONTACT") or _Contact().OSINT_CONTACT
            state["lookups"] = Lookups(data_dir / "profile_cache.sqlite", contact or None)
        return state["lookups"]

    async def local(key: str, fn, *args) -> dict:
        hit = memo.get(key)
        if hit is None:
            hit = await asyncio.to_thread(fn, ctx, *args)
            memo.put(key, hit)
        return hit

    @app.get("/api/profile/sources")
    def profile_sources() -> dict:
        return {"lookups": [{"id": s.id, "name": s.name, "licence": s.licence, "url": s.url,
                             "attribution": s.attribution, "ttl_ok_s": s.ttl_ok_s,
                             "ttl_miss_s": s.ttl_miss_s, "commercial": s.commercial,
                             "notes": s.notes} for s in SPECS.values()],
                "graph": ctx.graph.meta()}

    @app.get("/api/profile/aircraft/{key}")
    async def aircraft_profile(key: str) -> dict:
        hex_code, reg_key = build.parse_aircraft_key(key)
        if hex_code is None and reg_key is None:
            raise HTTPException(422, "expected an ICAO hex (6 hex digits) or reg:<registration>")
        L = lk()
        look: dict[str, dict] = {}
        if hex_code is None:  # registration only: resolve the hex through adsbdb
            res = L.peek("adsbdb", reg_key)
            look["adsbdb"] = res
            hx = (res.get("data") or {}).get("hex") if res.get("status") == "ok" else None
            if not hx:
                b = build.Builder("aircraft")
                b.lookup("adsbdb", res)
                return b.result(kind="aircraft", id=f"reg:{reg_key}", context="civil", order=[],
                                key={"reg": reg_key}, title=reg_key,
                                subtitle="Resolving registration…" if res.get("status") == "pending"
                                else "Registration not found (checked: adsbdb.com)")
            hex_code = hx.lower()
        look["hexdb"] = L.peek("hexdb", hex_code)
        look["adsbdb"] = L.peek("adsbdb", hex_code)
        ext_reg = ((look["hexdb"].get("data") or {}).get("reg")
                   or (look["adsbdb"].get("data") or {}).get("reg") or reg_key)
        loc = await local(f"air:{hex_code}:{ext_reg}", build.gather_aircraft, hex_code, ext_reg)
        reg = loc["reg"]
        cs = ((loc.get("track") or {}).get("name") or "").strip().upper()
        if cs and loc.get("live"):
            look["adsbdb_route"] = L.peek("adsbdb_route", cs)
        look["planespotters"] = L.peek("planespotters", f"{hex_code}|{build.norm_reg(reg) and reg or ''}")
        if look["planespotters"].get("status") in ("not_found", "error") and reg:
            look["commons"] = L.peek("commons", reg)
        return build.aircraft(ctx, hex_code, look, loc)

    @app.get("/api/profile/vessel/{key}")
    async def vessel_profile(key: str) -> dict:
        mmsi, imo = build.parse_vessel_key(key)
        if mmsi is None and imo is None:
            raise HTTPException(422, "expected an MMSI (9 digits) or imo:<7 digits>")
        loc = await local(f"sea:{mmsi}:{imo}", build.gather_vessel, mmsi, imo)
        look = {}
        if loc.get("imo"):
            look["commons"] = lk().peek("commons", f"IMO {loc['imo']}")
        return build.vessel(ctx, mmsi, look, loc)
