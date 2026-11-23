"""Carrier strike group activity API. Every item is a probabilistic hypothesis from public
signals (confidence, uncertainty radius, evidence) - never a confirmed position."""

import asyncio
import re

from fastapi import FastAPI, HTTPException, Query

from osint.naval.job import DISCLAIMER, CarrierJob, get_job

ID_RE = re.compile(r"^csg-[0-9a-f]{6,16}$")
Hours = Query(48, ge=1, le=168)


def _env(layer: str, res: dict, items: list) -> dict:
    return {"layer": layer, "generated_at": res["generated_at"], "hours": res["hours"],
            "count": len(items), "disclaimer": DISCLAIMER, "items": items}


def _slim(h: dict) -> dict:
    return {k: h[k] for k in (
        "id", "lat", "lon", "radius_km", "confidence", "label", "kinds", "independent_kinds",
        "indicator_breakdown",
        "candidate_names", "first_seen", "last_updated", "evidence_count", "evidence",
        "centre_basis", "ellipse", "air_track", "disclaimer") if k in h}


def mount_naval(app: FastAPI, runtime, job: CarrierJob | None = None) -> None:
    def j() -> CarrierJob:
        return job if job is not None else get_job(runtime)

    @app.get("/api/naval/carrier-surface")
    async def surface(hours: int = Hours) -> dict:
        res = await j().get(hours)
        return _env("naval_surface", res, res["surface"])

    @app.get("/api/naval/carriers")
    async def carriers(hours: int = Hours) -> dict:
        res = await j().get(hours)
        env = _env("naval_carriers", res, [_slim(h) for h in res["hypotheses"]])
        env["indicator_status"] = res["indicator_status"]
        return env

    @app.get("/api/naval/carriers/{hyp_id}")
    async def carrier(hyp_id: str) -> dict:
        if not ID_RE.match(hyp_id):
            raise HTTPException(404, "unknown hypothesis")
        jb = j()
        res = await jb.get(None)
        live = next((h for h in res["hypotheses"] if h["id"] == hyp_id), None)
        stored = await asyncio.to_thread(jb.nstore.get, hyp_id)
        if live is None and stored is None:
            raise HTTPException(404, "unknown hypothesis")
        out = dict(live) if live is not None else {
            **stored, "label": f"CSG? {stored['confidence']:.2f}", "stale": True}
        out["history"] = (stored or {}).get("history", [])
        out["disclaimer"] = DISCLAIMER
        return out

    @app.get("/api/naval/air-tracks")
    async def air_tracks(hours: int = Hours) -> dict:
        """Moving-point fits of carrier-aircraft launch/recovery/loiter fixes (all of them,
        including ones too weak to form a hypothesis), each with its 95 % ellipse at now."""
        res = await j().get(hours)
        return _env("naval_air_tracks", res, res.get("air_tracks", []))

    @app.get("/api/naval/indicators")
    async def indicators(hours: int = Hours, kind: str | None = None) -> dict:
        res = await j().get(hours)
        kinds = {k.strip() for k in (kind or "").split(",") if k.strip()}
        items = [e for e in res["evidence"] if not kinds or e["kind"] in kinds]
        env = _env("naval_indicators", res, items)
        env["indicator_status"] = res["indicator_status"]
        return env

    @app.get("/api/naval/eval")
    async def evaluation() -> dict:
        res = await j().get(None)
        return {"generated_at": res["generated_at"], "hours": res["hours"],
                "disclaimer": DISCLAIMER, **res["eval"]}
