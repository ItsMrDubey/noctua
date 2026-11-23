"""Terror Watch API (read-only: the worker's TerrorJob fetches, classifies and computes).

All times are UTC ISO-8601. Incident lists are ordered by event time (occurrence when the
article states it, else first report), never by processing time."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query

from osint.terror import board, countries, definition
from osint.terror.config import TerrorConfig, load_calendar
from osint.terror.groups import GroupKB
from osint.terror.sources import SOURCES
from osint.terror.sources.designations import LISTS
from osint.terror.store import get_store

LIVE = ("confirmed", "reported")


def _csv(v: str | None) -> list[str] | None:
    vals = [x.strip() for x in (v or "").split(",") if x.strip()]
    return vals or None


def load_kb(config_dir: Path, store) -> GroupKB:
    kb = GroupKB.load(config_dir / "terror_groups.yaml")
    kb.add_auto(store.get_meta("auto_groups") or [])
    return kb


def slim(i: dict) -> dict:
    return board._slim(i)


def mount_terror(app: FastAPI, runtime) -> None:
    settings = runtime.settings
    cache: dict = {"kb": None, "kb_at": 0.0}

    def store():
        return get_store(runtime)

    def kb() -> GroupKB:
        if cache["kb"] is None or time.monotonic() - cache["kb_at"] > 300:
            cache["kb"] = load_kb(settings.config_dir, store())
            cache["kb_at"] = time.monotonic()
        return cache["kb"]

    def cfg() -> TerrorConfig:
        return TerrorConfig.load(settings.config_dir / "terror.yaml")

    def coverage_days() -> float:
        cov = store().get_meta("news_coverage_start")
        if not cov:
            return 0.0
        dt = datetime.fromisoformat(cov)
        dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        return max(0.0, (datetime.now(UTC) - dt).total_seconds() / 86400)

    @app.get("/api/terror/definition")
    async def terror_definition() -> dict:
        return definition.as_dict()

    @app.get("/api/terror/status")
    async def terror_status() -> dict:
        def build():
            st = store().get_meta("status") or {"passes": 0}
            return {"job": st, "sources": store().sources(),
                    "news_coverage_start": store().get_meta("news_coverage_start")}
        return await asyncio.to_thread(build)

    @app.get("/api/terror/sources")
    async def terror_sources() -> dict:
        def build():
            states = {s["source_id"]: s for s in store().sources()}
            out = []
            for s in SOURCES:
                st = states.get(s.id) or {}
                out.append({"id": s.id, "name": s.name, "kind": s.kind, "urls": list(s.urls),
                            "interval_h": s.interval_h, "licence": s.licence,
                            "commercial": s.commercial, "attribution": s.attribution,
                            "enabled": s.enabled, "note": s.note,
                            "last_attempt": st.get("last_attempt"), "last_ok": st.get("last_ok"),
                            "ok": None if not st else bool(st.get("ok")),
                            "error": st.get("error"), "items": st.get("items")})
            return {"sources": out}
        return await asyncio.to_thread(build)

    @app.get("/api/terror/incidents")
    async def terror_incidents(status: str | None = Query(None), klass: str | None = None,
                               country: str | None = None, group: str | None = None,
                               days: int = Query(30, ge=1, le=3650),
                               follow_up: bool = False, full: bool = False,
                               limit: int = Query(1000, ge=1, le=5000)) -> dict:
        sts = _csv(status) or list(LIVE)
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()

        def build():
            items = store().incidents(statuses=sts, klass=_csv(klass), country=(country or
                                      "").upper() or None, group_id=group, since=since,
                                      include_follow_up=follow_up, limit=limit)
            return {"count": len(items), "since": since,
                    "order": "event time (occurrence if stated, else first report)",
                    "items": items if full else [slim(i) for i in items]}
        return await asyncio.to_thread(build)

    @app.get("/api/terror/incidents/{iid}")
    async def terror_incident(iid: str) -> dict:
        rec = await asyncio.to_thread(store().incident, iid)
        if rec is None:
            raise HTTPException(404, "no incident for this id")
        ext = await asyncio.to_thread(store().extraction, rec["event_id"])
        rec["extraction_meta"] = {k: (ext or {}).get(k) for k in (
            "url", "model", "cost_usd", "latency_s", "updated_at", "attempts")}
        return rec

    @app.get("/api/terror/feed")
    async def terror_feed(days: int = Query(30, ge=1, le=400),
                          types: str = "incidents,designations,levels,docs",
                          country: str | None = None, limit: int = Query(300, ge=1, le=2000)
                          ) -> dict:
        want = set(_csv(types) or [])
        now = datetime.now(UTC)
        since = (now - timedelta(days=days)).isoformat()
        cc = (country or "").upper() or None

        def build():
            s = store()
            out = []
            if "incidents" in want:
                for i in s.incidents(statuses=("confirmed", "reported", "possible"),
                                     country=cc, since=since, include_follow_up=True,
                                     limit=limit):
                    out.append({"type": "incident", "time": i.get("sort_time") or
                                i.get("first_reported_at"), "time_basis":
                                "occurred" if i.get("occurred_at") else "first report",
                                **slim(i)})
            if "designations" in want and not cc:
                for d in board.recent_designations(s, now, days):
                    out.append({"type": "designation", "time": d["date"], **d})
            if "levels" in want:
                for h in s.level_history(cc):
                    out.append({"type": "level_change", "time": h["observed_at"],
                                "time_basis": "detected", **h})
                for h in s.advisory_history(cc):
                    out.append({"type": "advisory_change", "time": h["observed_at"],
                                "time_basis": "detected", **h})
            if "docs" in want:
                for d in s.docs(since=since, limit=limit):
                    if cc and cc not in (d.get("countries") or []):
                        continue
                    out.append({"type": "doc", "time": d.get("published_at") or
                                d.get("first_seen"), "time_basis": d.get("date_basis"), **d})
            out = [o for o in out if (o.get("time") or "") >= since[:10]]
            out.sort(key=lambda o: o.get("time") or "", reverse=True)
            return {"count": len(out), "since": since, "items": out[:limit]}
        return await asyncio.to_thread(build)

    @app.get("/api/terror/board/global")
    async def terror_global() -> dict:
        def build():
            b = store().indicators("global")
            if b is None:
                c = cfg()
                from osint.terror import indicators as ind
                b = board.global_board(store(), kb(), now=datetime.now(UTC), cfg=c.indicators,
                                       coverage_days=coverage_days(),
                                       calendar=load_calendar(settings.config_dir /
                                                              "terror_calendar.yaml"),
                                       coverage=ind.coverage_months(store().ucdp()))
            return b
        return await asyncio.to_thread(build)

    @app.get("/api/terror/board/{cc}")
    async def terror_country(cc: str) -> dict:
        cc = cc.upper()
        if cc not in countries.table():
            raise HTTPException(404, "unknown country code")

        def build():
            b = store().indicators(f"country:{cc}")
            if b is None:
                c = cfg()
                cal = load_calendar(settings.config_dir / "terror_calendar.yaml")
                fn = board.us_panel if cc == "US" else None
                if fn:
                    from osint.terror import indicators as ind
                    b = fn(store(), kb(), now=datetime.now(UTC), cfg=c.indicators,
                           coverage_days=coverage_days(), calendar=cal,
                           coverage=ind.coverage_months(store().ucdp()))
                else:
                    b = board.country_board(store(), kb(), cc, now=datetime.now(UTC),
                                            cfg=c.indicators, coverage_days=coverage_days(),
                                            calendar=cal)
            return b
        return await asyncio.to_thread(build)

    @app.get("/api/terror/groups")
    async def terror_groups(q: str | None = None, include_auto: bool = True) -> dict:
        def build():
            k, s = kb(), store()
            desig: dict[str, set] = {}
            for d in s.designations():
                if d.get("group_id") and d.get("status") == "listed":
                    desig.setdefault(d["group_id"], set()).add(d["list_id"])
            counts: dict[str, int] = {}
            since = (datetime.now(UTC) - timedelta(days=365)).isoformat()
            for i in s.incidents(statuses=LIVE, since=since, limit=20000):
                g = (i.get("group") or {})
                if g.get("kind") == "link":
                    counts[g["group_id"]] = counts.get(g["group_id"], 0) + 1
            out = []
            ql = (q or "").casefold()
            for g in k.groups.values():
                if g.auto and not include_auto:
                    continue
                if ql and ql not in g.name.casefold() and not any(
                        ql in a.casefold() for a in g.aliases):
                    continue
                out.append({**g.as_dict(), "lists": sorted(desig.get(g.id, [])),
                            "incidents_365d": counts.get(g.id, 0)})
            out.sort(key=lambda g: (g["auto"], -g["incidents_365d"], g["name"]))
            return {"count": len(out), "items": out}
        return await asyncio.to_thread(build)

    @app.get("/api/terror/groups/{gid}")
    async def terror_group(gid: str) -> dict:
        page = await asyncio.to_thread(lambda: board.group_page(store(), kb(), gid,
                                                                now=datetime.now(UTC)))
        if page is None:
            raise HTTPException(404, "unknown group")
        return page

    @app.get("/api/terror/designations")
    async def terror_designations(list_id: str | None = None, q: str | None = None,
                                  kind: str | None = None,
                                  limit: int = Query(500, ge=1, le=5000)) -> dict:
        def build():
            items = store().designations(list_id=list_id, kind=kind)
            if q:
                ql = q.casefold()
                items = [d for d in items if ql in d["name"].casefold() or
                         any(ql in a.casefold() for a in d.get("aliases") or [])]
            return {"count": len(items), "items": items[:limit], "lists": LISTS}
        return await asyncio.to_thread(build)

    @app.get("/api/terror/levels")
    async def terror_levels() -> dict:
        return {"items": await asyncio.to_thread(store().levels),
                "history": await asyncio.to_thread(store().level_history)}

    @app.get("/api/terror/advisories")
    async def terror_advisories() -> dict:
        return {"items": await asyncio.to_thread(store().advisories)}

    @app.get("/api/terror/eval")
    async def terror_eval() -> dict:
        path = Path(settings.data_dir) / "terror_eval.json"
        if not path.is_file():
            return {"available": False, "basis": "run `osint terror-eval` to produce a report"}
        return json.loads(path.read_text(encoding="utf-8"))
