"""Signals & space API: GNSS interference, emergency squawks, satellites, internet outages,
space weather, aurora and radiation. Serves the worker's live snapshots
(``data_dir/signals/*.json``, see ``osint.signals``) plus lake history; no outbound requests."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Query

from osint.signals.cache import SignalReader
from osint.signals.countries import get_countries
from osint.signals.gnss import DEFAULTS as GNSS_DEFAULTS
from osint.signals.gnss import cell_item
from osint.signals.radiation import assess
from osint.signals.satellites import select


def _attrs(raw: str | None) -> dict:
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def gnss_history(lake, hours: int, live: dict | None, now: float | None = None) -> dict:
    """Per-cell sums of the closed hourly buckets in the lake over the last ``hours``, plus
    the live window's counts for cells (or hours) the lake does not hold yet."""
    end = datetime.fromtimestamp(now or time.time(), UTC)
    start = end - timedelta(hours=hours)
    rows = lake.query(
        "SELECT id, arg_max(attrs, version) FROM observations WHERE type = 'gnss' "
        "AND ts >= ? AND ts <= ? GROUP BY id", [start, end], start=start, end=end)
    sums: dict[str, list[int]] = {}
    for _, raw in rows:
        a = _attrs(raw)
        cell = a.get("cell")
        if not cell:
            continue
        s = sums.setdefault(cell, [0, 0])
        s[0] += int(a.get("n") or 0)
        s[1] += int(a.get("bad") or 0)
    for c in (live or {}).get("cells") or []:
        s = sums.setdefault(c["h3"], [0, 0])
        s[0] += c["n"]
        s[1] += c["bad"]
    thresholds = {**GNSS_DEFAULTS, **((live or {}).get("thresholds") or {})}
    cells = [cell_item(cell, n, bad, thresholds) for cell, (n, bad) in sums.items()
             if n >= int(thresholds["min_aircraft"])]
    cells.sort(key=lambda c: (-c["pct"], -c["n"]))
    return {"hours": hours, "cells": cells, "hourly_rows": len(rows)}


def radiation_items(snap: dict | None, base: dict | None) -> list[dict]:
    stations = (base or {}).get("stations") or {}
    thresholds = (base or {}).get("thresholds")
    out = []
    for s in (snap or {}).get("stations") or []:
        b = stations.get(s["id"])
        out.append({**s, "baseline": b[0] if b else None, "baseline_n": b[1] if b else 0,
                    "status": assess(float(s["value"]), b, thresholds)})
    return out


def status_summary(r: SignalReader, now: float | None = None) -> dict:
    now = now or time.time()
    gnss = r.get("gnss") or {}
    cells = gnss.get("cells") or []
    sq = r.get("squawks") or {}
    sqi = sq.get("items") or []
    out_ = r.get("outages") or {}
    countries = out_.get("countries") or []
    sw = r.get("spaceweather") or {}
    rad = radiation_items(r.get("radiation"), r.get("radiation_baseline"))
    sats = r.get("satellites") or {}
    alerts = (r.get("alerts") or {}).get("items") or []
    return {
        "now": now,
        "gnss": {"updated": gnss.get("updated"), "cells": len(cells),
                 "high": sum(1 for c in cells if c["level"] == "high"),
                 "medium": sum(1 for c in cells if c["level"] == "medium"),
                 "samples": gnss.get("samples")},
        "squawks": {"updated": sq.get("updated"),
                    "active": sum(1 for e in sqi if e.get("active")), "day": len(sqi)},
        "outages": {"updated": out_.get("updated"), "countries": len(countries),
                    "ongoing": sum(1 for c in countries if c.get("ongoing")),
                    "high": sum(1 for c in countries if c.get("level") == "high")},
        "spaceweather": {"updated": sw.get("updated"), "kp_now": sw.get("kp_now"),
                         "g_now": sw.get("g_now"),
                         "scales": (sw.get("scales") or {}).get("now"),
                         "solar_wind": sw.get("solar_wind"),
                         "latest_alert": (sw.get("alerts") or [None])[0]},
        "radiation": {"updated": (r.get("radiation") or {}).get("updated"),
                      "stations": len(rad),
                      "elevated": sum(1 for s in rad if s["status"] in ("elevated", "alert"))},
        "satellites": {"updated": sats.get("updated"), "objects": len(sats.get("objects") or {}),
                       "groups": {g: (v or {}).get("count") for g, v in
                                  (sats.get("groups") or {}).items()}},
        "alerts": {"count": len(alerts), "latest": alerts[0]["ts"] if alerts else None},
    }


def mount_signals(app: FastAPI, runtime) -> None:
    reader = SignalReader(runtime.settings.data_dir)

    def snap(name: str) -> dict:
        return reader.get(name) or {}

    @app.get("/api/signals/status")
    def signals_status() -> dict:
        return status_summary(reader)

    @app.get("/api/signals/gnss")
    async def gnss(hours: int = Query(1, ge=1, le=168)) -> dict:
        live = snap("gnss")
        meta = {k: live.get(k) for k in ("updated", "res", "window_min", "thresholds",
                                         "regions", "samples", "rate_limited")}
        if hours <= 1:
            return {**meta, "hours": 1, "cells": live.get("cells") or []}
        hist = await asyncio.to_thread(gnss_history, runtime.lake, hours, live)
        return {**meta, **hist}

    @app.get("/api/signals/squawks")
    def squawks() -> dict:
        s = snap("squawks")
        return {"updated": s.get("updated"), "items": s.get("items") or []}

    @app.get("/api/signals/alerts")
    def alerts(since: float = 0.0) -> dict:
        s = snap("alerts")
        return {"updated": s.get("updated"),
                "items": [a for a in s.get("items") or [] if a["ts"] > since]}

    @app.get("/api/signals/outages")
    async def outages(shapes: bool = False) -> dict:
        s = snap("outages")
        countries = s.get("countries") or []
        out = {"updated": s.get("updated"), "window_h": s.get("window_h"),
               "countries": countries, "events": s.get("events") or []}
        if shapes:
            ref = await asyncio.to_thread(get_countries)
            out["shapes"] = {c["cc"]: ref.shape(c["cc"]) for c in countries}
        return out

    @app.get("/api/signals/spaceweather")
    def spaceweather() -> dict:
        return snap("spaceweather")

    @app.get("/api/signals/aurora")
    def aurora() -> dict:
        return snap("aurora")

    @app.get("/api/signals/radiation")
    def radiation() -> dict:
        s = snap("radiation")
        items = radiation_items(s, snap("radiation_baseline"))
        return {"updated": s.get("updated"), "networks": s.get("networks") or {},
                "items": items}

    @app.get("/api/signals/satellites")
    def satellites(groups: str = "stations", limit: int = Query(20000, ge=1, le=50000)) -> dict:
        want = [g.strip() for g in groups.split(",") if g.strip()]
        return select(reader.get("satellites"), want, limit)
