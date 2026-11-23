"""Imagery API: before/after crop metadata and image files for an event."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from osint.api.security import sign_path
from osint.imagery.config import ImageryConfig
from osint.imagery.job import EVENT_ID_RE, get_store

NAME_RE = re.compile(r"^(before|after)\.(png|jpg)$")
MEDIA = {"png": "image/png", "jpg": "image/jpeg"}
COPERNICUS = "Contains modified Copernicus Sentinel data {year}"
LANDSAT = "Landsat imagery courtesy of USGS"
# Sentinel-2 constellation revisit at the equator (2 satellites); higher latitudes see
# overlapping orbits more often, so a projection from a known pass is an upper bound.
S2_REVISIT_DAYS = 5


def attribution(collection: str | None, acquired_at: str | None) -> str | None:
    if not collection:
        return None
    if collection.startswith("sentinel"):
        return COPERNICUS.format(year=(acquired_at or "")[:4] or "")
    if collection.startswith("landsat"):
        return LANDSAT
    return None


def _valid_id(event_id: str) -> str:
    if not EVENT_ID_RE.match(event_id):
        raise HTTPException(404, "unknown event")
    return event_id


def _asset_out(event_id: str, a: dict, token: str = "") -> dict:
    url = None
    if a.get("path") and a["status"] == "ok":
        # ?v= changes when the crop is redone (re-centred), so a cached copy is never shown
        ver = re.sub(r"\D", "", a.get("updated_at") or "")[:14]
        url = sign_path(token, f"/api/imagery/{event_id}/{a['role']}."
                               f"{a['path'].rsplit('.', 1)[-1]}" + (f"?v={ver}" if ver else ""))
    return {
        "role": a["role"], "status": a["status"], "collection": a["collection"],
        "scene_id": a["scene_id"], "acquired_at": a["acquired_at"], "cloud_pct": a["cloud_pct"],
        "change_score": a["change_score"], "url": url, "bounds": a["bounds"],
        "kind": (a["bounds"] or {}).get("kind"), "error": a["error"],
        "attribution": attribution(a["collection"], a["acquired_at"]) if url else None,
        "attempts": a.get("attempts"), "next_try_at": a.get("next_try_at"),
        "checked_at": a.get("updated_at"), "target": (a["bounds"] or {}).get("target"),
    }


def _ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def watch_status(assets: list[dict], t_start: str | None, cfg: ImageryConfig,
                 now: datetime | None = None) -> dict:
    """Why there is (no) post-event image: the real job state plus a labelled projection
    of the next Sentinel-2 pass from the latest known pass over this point."""
    now = now or datetime.now(UTC)
    by = {a["role"]: a for a in assets}
    after = by.get("after") or {}
    out: dict = {"max_cloud_pct": cfg.max_cloud_pct, "retry_after_h": cfg.retry_after_h,
                 "watch_until": None, "state": "not_scheduled", "next_pass_est": None}
    t0 = _ts(t_start)
    if t0 is not None:
        out["watch_until"] = (t0 + timedelta(days=cfg.event_age_days)).isoformat(
            timespec="seconds")
    if not assets:
        return out
    out["state"] = {"ok": "acquired", "awaiting_after": "awaiting",
                    "failed": "failed"}.get(after.get("status"), after.get("status") or "queued")
    known = [_ts(a.get("acquired_at")) for a in assets
             if a.get("acquired_at") and (a.get("collection") or "").startswith("sentinel-2")]
    known = [k for k in known if k is not None]
    if out["state"] == "awaiting" and known:
        nxt = max(known)
        while nxt <= now:
            nxt += timedelta(days=S2_REVISIT_DAYS)
        out["next_pass_est"] = nxt.isoformat(timespec="minutes")
        out["next_pass_basis"] = (f"latest known pass + {S2_REVISIT_DAYS}-day Sentinel-2 "
                                  "revisit (estimate)")
    if out["watch_until"] and _ts(out["watch_until"]) < now and out["state"] == "awaiting":
        out["state"] = "expired"
    return out


def mount_imagery(app: FastAPI, runtime) -> None:
    store = get_store(runtime)
    root = (runtime.settings.data_dir / "imagery").resolve()
    config_dir = getattr(runtime.settings, "config_dir", None)
    cfg = ImageryConfig.load(config_dir / "imagery.yaml") if config_dir else ImageryConfig()

    @app.get("/api/imagery/{event_id}")
    async def imagery(event_id: str) -> dict:
        _valid_id(event_id)
        return await asyncio.to_thread(imagery_payload, runtime, store, cfg, event_id)

    @app.get("/api/imagery/{event_id}/{name}")
    async def imagery_file(event_id: str, name: str) -> FileResponse:
        _valid_id(event_id)
        m = NAME_RE.match(name)
        if not m:
            raise HTTPException(404, "unknown image")
        role, ext = m.groups()
        rows = await asyncio.to_thread(store.get_assets, event_id)
        row = next((r for r in rows if r["role"] == role and r["status"] == "ok"
                    and r["path"] and r["path"].endswith("." + ext)), None)
        if row is None:
            raise HTTPException(404, "no such image")
        path = (runtime.settings.data_dir / row["path"]).resolve()
        if not path.is_relative_to(root) or not Path(path).is_file():
            raise HTTPException(404, "no such image")
        return FileResponse(path, media_type=MEDIA[ext],
                            headers={"Cache-Control": "public, max-age=3600"})


def imagery_payload(runtime, store, cfg: ImageryConfig, event_id: str) -> dict:
    """GET /api/imagery/{id} body (also embedded by the intel aggregation)."""
    rows = store.get_assets(event_id)
    assets = [_asset_out(event_id, a, getattr(runtime.settings, "api_token", "")) for a in rows]
    attributions = sorted({a["attribution"] for a in assets if a["attribution"]})
    get_event = getattr(getattr(runtime, "store", None), "get_event", None)
    ev = get_event(event_id) if get_event else None
    return {"event_id": event_id, "assets": assets, "attributions": attributions,
            "watch": watch_status(rows, (ev or {}).get("t_start"), cfg)}
