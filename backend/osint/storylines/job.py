"""StorylineJob (worker only): links recent events into storylines and stores them.

Every ``interval_s`` it checks a cheap change marker of the events table
(``SqliteStore.events_fingerprint``); when events changed it loads the window
(``features.load_nodes``, one indexed read plus side lookups) and then

* **incremental pass**: only events not seen before are assigned (``Assembler.add``),
  sensor events attached; events whose row changed get their fresh features but keep their
  thread; an event that is gone (merged away, deleted) forces a full pass;
* **full pass** (start, every ``rebuild_h``, or forced): the whole window is re-derived
  deterministically (``assemble.build``), so incremental drift never accumulates.

The derived threads (roles, title, theatre, span by best-known event time) are synced to
``storylines.sqlite`` with stable ids (``assign_ids``); unchanged threads are not rewritten.
Linking never runs on an API request.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from datetime import datetime, timedelta

from osint.core.geo import utcnow
from osint.storylines import assemble
from osint.storylines.config import StorylineConfig
from osint.storylines.features import load_nodes
from osint.storylines.link import Node
from osint.storylines.store import StorylineStore, get_store

log = logging.getLogger(__name__)

STATUS_RANK = {"reported": 0, "multi-source": 1, "sensor-corroborated": 2, "official": 3}
STATUS_KEY = "status"


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat(timespec="seconds")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def member_row(n: Node, role: tuple[str, str]) -> dict:
    return {"event_id": n.id, "role": role[0], "role_basis": role[1], "origin": n.origin,
            "category": n.category, "title": n.title[:300], "t": _iso(n.t),
            "t_basis": n.t_basis, "t_precision": n.t_precision, "t_lo": _iso(n.t_lo),
            "t_hi": _iso(n.t_hi), "first_report": _iso(n.first_report),
            "t_end": _iso(n.t_end), "lat": n.lat, "lon": n.lon, "precision": n.precision,
            "place_name": n.place_name, "country": n.country, "status": n.status,
            "mentions": n.mentions, "sources": n.sources, "detector": n.detector}


def derive(story: assemble.Story, country_names: dict[str, str] | None = None) -> dict:
    """The stored record of a thread (without its id): members with roles, span by
    best-known event time, title (+ basis), theatre, places, actors, bounds."""
    roles = assemble.roles(story)
    members = sorted(story.members.values(), key=lambda n: (n.t, n.id))
    news = [n for n in members if n.origin == "news"]
    rep = assemble.representative(story)
    origin = news[0]
    theatre, label = assemble.theatre_of(story, country_names)
    first = members[0]
    places: dict[str, dict] = {}
    for n in members:
        if n.place_name and n.lat is not None:
            p = places.setdefault(n.place_name, {"name": n.place_name, "lat": n.lat,
                                                 "lon": n.lon, "precision": n.precision,
                                                 "events": 0})
            p["events"] += 1
    actors = Counter(a for n in news for a in n.actors)
    fine = [n for n in members if n.lat is not None and n.precision != "country"] or \
        [n for n in members if n.lat is not None]
    bbox = (min(n.lat for n in fine), min(n.lon for n in fine), max(n.lat for n in fine),
            max(n.lon for n in fine)) if fine else (None, None, None, None)
    anchor = rep if rep.lat is not None else (fine[0] if fine else None)
    links = [{"a": lk.a, "b": lk.b, "score": lk.score, "reasons": lk.reasons}
             for lk in sorted(story.links.values(), key=lambda x: (-x.score, x.a, x.b))
             if lk.a in story.members and lk.b in story.members]
    rec = {
        "title": rep.title[:300],
        "title_basis": "title of the most-reported member event ("
                       + _plural(rep.sources, "outlet") + ", " + _plural(rep.mentions, "report") + ")",
        "theatre": theatre, "theatre_label": label,
        "t_first": _iso(first.t), "t_first_basis": first.t_basis,
        "t_last": _iso(max(max(n.t, n.t_end) for n in members)),
        "first_report": _iso(min((n.first_report for n in news if n.first_report),
                                 default=None)),
        "last_report": _iso(max(n.t_end for n in news)),
        "n_events": len(members), "n_news": len(news), "n_sensor": len(members) - len(news),
        "categories": [c for c, _ in Counter(n.category for n in members).most_common()],
        "places": sorted(places.values(), key=lambda p: (-p["events"], p["name"]))[:12],
        "actors": [a for a, _ in actors.most_common(10)],
        "countries": sorted({(n.country or "").upper() for n in members if n.country}),
        "roles": dict(Counter(r for r, _ in roles.values())),
        "min_lat": bbox[0], "min_lon": bbox[1], "max_lat": bbox[2], "max_lon": bbox[3],
        "lat": anchor.lat if anchor else None, "lon": anchor.lon if anchor else None,
        "origin_event_id": origin.id, "representative_event_id": rep.id,
        "top_status": max((n.status for n in news), key=lambda s: STATUS_RANK.get(s, -1)),
        "members": [member_row(n, roles[n.id]) for n in members],
        "links": links,
    }
    rec["fingerprint"] = assemble.fingerprint(rec)
    return rec


def assign_ids(records: list[dict], previous: dict[str, set[str]]
               ) -> tuple[list[dict], dict[str, str]]:
    """Give each record a stable id: the stored thread it overlaps most (shared members,
    then Jaccard) when that id is still free, else ``SL<origin event id>``. Stored threads
    that were not reused redirect to the record holding most of their members."""
    taken: set[str] = set()
    order = sorted(records, key=lambda r: (-r["n_events"], r["t_first"], r["origin_event_id"]))
    for r in order:
        ids = {m["event_id"] for m in r["members"]}
        best, key = None, None
        for sid, old in previous.items():
            inter = len(ids & old)
            if not inter or sid in taken:
                continue
            k = (inter, inter / len(ids | old), sid)
            if key is None or k > key:
                best, key = sid, k
        if best is None:
            best = "SL" + r["origin_event_id"]
            n = 2
            while best in taken:
                best = f"SL{r['origin_event_id']}-{n}"
                n += 1
        taken.add(best)
        r["id"] = best
    redirects: dict[str, str] = {}
    for sid, old in previous.items():
        if sid in taken:
            continue
        hit = max(records, key=lambda r: (len(old & {m["event_id"] for m in r["members"]}),
                                          r["id"]), default=None)
        if hit is not None and old & {m["event_id"] for m in hit["members"]}:
            redirects[sid] = hit["id"]
    return records, redirects


class StorylineJob:
    def __init__(self, store, sstore: StorylineStore, cfg: StorylineConfig, *,
                 translations=None, country_names: dict[str, str] | None = None,
                 clock=utcnow) -> None:
        self.store = store
        self.sstore = sstore
        self.cfg = cfg
        self.translations = translations
        self.country_names = country_names
        self.clock = clock
        self.asm: assemble.Assembler | None = None
        self._fp: dict | None = None
        self._built_at: datetime | None = None
        self.passes = 0
        self.last_run_at: str | None = None
        self.last_error: str | None = None
        self.last: dict = {}

    def _since(self, now: datetime) -> datetime:
        return now - timedelta(days=self.cfg.window_days)

    def run_once(self, now: datetime | None = None, *, force_full: bool = False) -> dict:
        """One pass (call from a worker thread). Returns its cost and counts."""
        now = now or self.clock()
        t0 = time.perf_counter()
        fp = self.store.events_fingerprint(self._since(now))
        due = self._built_at is None or force_full or \
            now - self._built_at >= timedelta(hours=self.cfg.rebuild_h)
        if fp == self._fp and not due:
            return {"mode": "skipped", "seconds": round(time.perf_counter() - t0, 4)}
        nodes = load_nodes(self.store, self._since(now), translations=self.translations)
        t_load = time.perf_counter() - t0
        mode = "full" if due or self.asm is None else self._incremental(nodes)
        if mode == "full":
            self.asm = assemble.build(nodes, self.cfg)
            self._built_at = now
        t_asm = time.perf_counter() - t0 - t_load
        threads = self.asm.threads()
        records = [derive(s, self.country_names) for s in threads]
        records, redirects = assign_ids(records, self.sstore.memberships())
        for s, r in zip(threads, records, strict=True):
            s.id = r["id"]
        res = self.sstore.sync(records, redirects)
        self._fp = fp
        out = {"mode": mode, "events": len(nodes), "storylines": len(records),
               "members": sum(r["n_events"] for r in records), **res,
               "redirects": len(redirects), "pairs_scored": self.asm.stats["pairs_scored"],
               "load_s": round(t_load, 3), "assemble_s": round(t_asm, 3),
               "seconds": round(time.perf_counter() - t0, 3)}
        self.last = out
        return out

    def _incremental(self, nodes: list[Node]) -> str:
        """Assign only new events; refresh changed ones in place. 'full' when an event the
        index holds is gone (a merge or deletion re-shapes threads)."""
        asm = self.asm
        by = {n.id: n for n in nodes}
        if any(i not in by for i in asm.nodes):
            return "full"
        fresh_news, fresh_sensor = [], []
        for n in nodes:
            old = asm.nodes.get(n.id)
            if old is None:
                (fresh_news if n.origin == "news" else fresh_sensor).append(n)
            elif old.updated != n.updated:
                asm.nodes[n.id] = n
                story = asm.stories.get(asm.story_of.get(n.id, -1))
                if story is not None and n.id in story.members:
                    story.members[n.id] = n
        for n in fresh_news:
            asm.corpus.add(n)
        for n in sorted(fresh_news, key=lambda x: (x.t, x.id)):
            asm.add(n)
        asm.attach_sensors(fresh_sensor)
        return "incremental"

    async def run(self) -> None:
        await asyncio.sleep(self.cfg.initial_delay_s)
        while True:
            if self.cfg.enabled:
                try:
                    out = await asyncio.to_thread(self.run_once)
                    self.last_error = None
                    if out["mode"] != "skipped":
                        self.last_run_at = self.clock().isoformat(timespec="seconds")
                        log.info("storylines pass: %s", out)
                        self.sstore.set_meta(STATUS_KEY, self.status())
                except Exception as exc:  # the next pass retries; never kill the worker
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("storyline pass failed")
                self.passes += 1
            await asyncio.sleep(self.cfg.interval_s)

    def status(self) -> dict:
        return {"enabled": self.cfg.enabled, "passes": self.passes,
                "last_run_at": self.last_run_at, "last_error": self.last_error,
                "last_pass": self.last}


# ------------------------------------------------------------------- wiring


def country_names(ref_dir=None) -> dict[str, str]:
    """ISO alpha-2 -> name from the Natural Earth file the reference fetch downloads (empty
    when absent: theatre labels then show the code)."""
    try:
        from osint.signals.countries import Countries

        c = Countries(ref_dir) if ref_dir else Countries()
        return {cc: v[2] for cc, v in c.points.items()}
    except Exception:  # noqa: BLE001 — optional reference data
        log.info("country names unavailable; theatre labels use codes")
        return {}


def build_job(runtime) -> StorylineJob:
    settings = runtime.settings
    cfg = StorylineConfig.load(settings.config_dir / "storylines.yaml")
    tr = None
    try:
        from osint.translate.job import get_store as translation_store

        tr = translation_store(runtime)
    except Exception:  # English titles are an improvement, not a requirement
        log.exception("translation cache unavailable for storylines")
    return StorylineJob(runtime.store, get_store(runtime), cfg, translations=tr,
                        country_names=country_names())


def start(runtime) -> asyncio.Task:
    """Create the job (as runtime.storyline_job) and start its loop as a task."""
    job = build_job(runtime)
    runtime.storyline_job = job
    task = asyncio.create_task(job.run(), name="storylines")
    runtime._tasks.append(task)
    return task
