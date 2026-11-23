"""CelesTrak GP element sets (OMM JSON) for the satellite layer, refreshed a few times a day.

CelesTrak updates GP data about every 2 hours and blocks clients that download the same data
more often, so a group fetched less than ``min_refresh_s`` ago (per the cursor, which
survives restarts) is not fetched again; its rows come from the cached snapshot instead.
Derived groups (``derived``: name -> object-name prefix, e.g. starlink -> STARLINK) are cut
from ``active`` so the 4 MB Starlink set is not downloaded twice.

Output: ``signals/satellites.json`` (compact rows by NORAD id + group membership; the client
propagates with satellite.js). No lake rows: element sets are not observations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from osint.core.collector import CollectorContext, PollCollector, register
from osint.core.models import Batch
from osint.signals.cache import SignalReader, write_signal
from osint.signals.satellites import FIELDS, derive, parse_group

log = logging.getLogger(__name__)

GP_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=json"
DEFAULT_GROUPS = ("active", "stations", "visual", "military", "weather", "gnss", "geo",
                  "last-30-days")
DEFAULT_DERIVED = {"starlink": "STARLINK", "oneweb": "ONEWEB", "kuiper": "KUIPER"}
UNCHANGED_MARK = "has not updated since your last successful download"


class Unchanged(Exception):
    """CelesTrak's 403 "GP data has not updated since your last successful download"."""


@register
class CelestrakCollector(PollCollector):
    name = "celestrak"
    domain = "space"
    licence = "CelesTrak GP data (free; credit CelesTrak)"
    commercial_ok = True
    default_interval_s = 21600.0
    poll_timeout_s = 300.0

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.groups = [str(g) for g in cfg.get("groups") or DEFAULT_GROUPS]
        self.derived = {str(k): str(v) for k, v in (cfg.get("derived") or DEFAULT_DERIVED).items()}
        self.min_refresh_s = float(cfg.get("min_refresh_s", 7200))
        self.spacing_s = float(cfg.get("spacing_s", 2.0))
        self.url = str(cfg.get("url") or GP_URL)
        self.sleep = asyncio.sleep

    async def _fetch(self, ctx: CollectorContext, group: str) -> list[list]:
        r = await ctx.http.get(self.url.format(group=group), timeout=120)
        text = " ".join(r.text.split()) if r.status_code == 403 else ""
        if UNCHANGED_MARK in text:  # (the message is wrapped over several lines)
            raise Unchanged(text[:200])
        r.raise_for_status()
        try:
            payload = r.json()
        except (json.JSONDecodeError, ValueError):
            # "GP data has not updated since your last successful download ..." etc.
            raise ValueError(r.text.strip()[:200] or "non-JSON response") from None
        rows = parse_group(payload)
        if not rows:
            raise ValueError(f"no element sets in {group}")
        return rows

    async def poll(self, ctx: CollectorContext) -> Batch:
        data_dir = ctx.settings.data_dir
        cursor = ctx.get_cursor(self.name) or {}
        fetched: dict[str, float] = dict(cursor.get("fetched") or {})
        old = SignalReader(data_dir).get("satellites") or {}
        objects: dict[str, list] = dict(old.get("objects") or {})
        membership: dict[str, list[int]] = dict(old.get("membership") or {})
        status: dict[str, dict] = dict(old.get("groups") or {})
        now = time.time()
        due = [g for g in self.groups
               if g not in membership or now - float(fetched.get(g, 0)) >= self.min_refresh_s]
        ok, unchanged, errors = 0, 0, []
        for i, group in enumerate(due):
            if i:
                await self.sleep(self.spacing_s)
            try:
                rows = await self._fetch(ctx, group)
            except Unchanged as exc:  # nothing new upstream: retry after min_refresh_s
                unchanged += 1
                fetched[group] = now
                status[group] = {**status.get(group, {}), "unchanged": str(exc)[:200]}
                continue
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"{group}: {exc}")
                status[group] = {**status.get(group, {}), "ok": False,
                                 "error": str(exc)[:200]}
                continue
            ok += 1
            for r in rows:
                objects[str(r[0])] = r
            membership[group] = [r[0] for r in rows]
            fetched[group] = now
            status[group] = {"ok": True, "count": len(rows), "fetched_at": now}
        if due and not ok and not unchanged:
            raise RuntimeError("celestrak: " + "; ".join(errors))
        for err in errors:
            log.warning("celestrak group failed: %s", err)
        active = [objects[str(n)] for n in membership.get("active", []) if str(n) in objects]
        for g, ids in derive(active, self.derived).items():
            membership[g] = ids
            status[g] = {"ok": True, "count": len(ids), "derived_from": "active",
                         "fetched_at": fetched.get("active")}
        keep = {str(n) for ids in membership.values() for n in ids}
        objects = {k: v for k, v in objects.items() if k in keep}
        if ok or unchanged or not old:
            write_signal(data_dir, "satellites", {
                "updated": now, "fields": list(FIELDS), "groups": status,
                "membership": membership, "objects": objects})
            ctx.set_cursor(self.name, {"fetched": fetched})
        return Batch(source=self.name)
