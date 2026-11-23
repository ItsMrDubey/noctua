"""TerrorJob (worker): official sources -> LLM extraction of candidates -> incidents ->
indicators, every ``interval_s``. Writes data_dir/terror.sqlite; the API only reads it.

Cost: only candidate events (terror vocabulary in a headline, not excluded by a rule) are
sent to the LLM, once each, within hourly / daily caps; every call is logged with its cost.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from osint.terror import baseline as ucdp
from osint.terror import classify as cls
from osint.terror import dedupe, extract, text
from osint.terror.config import TerrorConfig, load_calendar
from osint.terror.groups import GroupKB, name_variants
from osint.terror.sources import BOT_UA, BROWSER_UA, BY_ID, feeds, official
from osint.terror.sources import designations as des
from osint.terror.store import TerrorStore, get_store

log = logging.getLogger(__name__)
STATUS_KEY = "status"
AUTO_GROUP_LISTS = {"us_fto", "uk_proscribed", "eu_terr", "eu_isil_aq"}


def utcnow() -> datetime:
    return datetime.now(UTC)


class TerrorJob:
    def __init__(self, event_store, store: TerrorStore, cfg: TerrorConfig, kb: GroupKB, *,
                 base_store=None, backend=None, geocoder=None, fetch_cfg=None,
                 alterlab_key: str = "", calendar: list[dict] | None = None,
                 cache_dir: Path | None = None, http_factory=None, limiter=None,
                 clock=time.time) -> None:
        self.events, self.store, self.cfg, self.kb = event_store, store, cfg, kb
        self.base_store = base_store
        self.backend = backend
        self.geocoder = geocoder
        self.fetch_cfg = fetch_cfg
        self.alterlab_key = alterlab_key
        self.calendar = calendar or []
        self.cache_dir = cache_dir
        self.clock = clock
        self.http_factory = http_factory or (lambda: httpx.AsyncClient(
            follow_redirects=True, timeout=cfg.sources.timeout_s,
            headers={"User-Agent": BOT_UA}))
        if limiter is None:
            from osint.llm import CallRateLimiter
            limiter = CallRateLimiter(cfg.llm.max_calls_per_hour, cfg.llm.max_calls_per_day,
                                      clock=clock, history=store.call_times_since(clock() - 86400))
        self.limiter = limiter
        self.passes = 0
        self.last_error: str | None = None
        self.last_pass: dict = {}

    # =============================================================== sources

    async def _get(self, http, sid: str, url: str, *, conditional: bool = True
                   ) -> tuple[int, bytes | None]:
        spec = BY_ID.get(sid)
        headers = {"User-Agent": BROWSER_UA if spec and spec.browser_ua else BOT_UA}
        st = self.store.source_state(sid) or {}
        if conditional and st.get("etag"):
            headers["If-None-Match"] = st["etag"]
        if conditional and st.get("last_modified"):
            headers["If-Modified-Since"] = st["last_modified"]
        r = await http.get(url, headers=headers)
        if r.status_code == 304:
            return 304, None
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
        self._validators[sid] = (r.headers.get("etag"), r.headers.get("last-modified"))
        return r.status_code, r.content

    def _due(self, sid: str, now: datetime) -> bool:
        spec = BY_ID[sid]
        if not spec.enabled:
            return False
        st = self.store.source_state(sid)
        if not st or not st.get("last_attempt"):
            return True
        last = datetime.fromisoformat(st["last_attempt"])
        interval = spec.interval_h if st.get("ok") else min(spec.interval_h, 1.0)
        return now - last >= timedelta(hours=interval)

    def _cache(self, sid: str, body: bytes, ext: str) -> None:
        if self.cache_dir is None:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_dir / f"{sid}.{ext}.part"
            tmp.write_bytes(body)
            tmp.replace(self.cache_dir / f"{sid}.{ext}")
        except OSError:
            log.debug("cache write failed for %s", sid, exc_info=True)

    async def refresh_sources(self, now: datetime | None = None, only: list[str] | None = None
                              ) -> dict:
        now = now or utcnow()
        if not self.cfg.sources.enabled:
            return {}
        self._validators: dict[str, tuple] = {}
        results: dict[str, str] = {}
        handlers = {
            "mi5_threat_level": self._src_level, "vigipirate": self._src_level,
            "nctv_threat_level": self._src_level, "asio_threat_level": self._src_level,
            "dhs_ntas": self._src_ntas, "state_advisories": self._src_advisories,
            "us_fto": self._src_designations, "ofac_sdgt": self._src_designations,
            "un_1267_1988": self._src_designations, "uk_proscribed": self._src_designations,
            "eu_terrorist_list": self._src_designations, "doj_press": self._src_doj,
            "europol_news": self._src_docs, "gnet_research": self._src_docs,
            "ucdp_osv": self._src_ucdp}
        async with self.http_factory() as http:
            for sid, fn in handlers.items():
                if only is not None and sid not in only:
                    continue
                if only is None and not self._due(sid, now):
                    continue
                try:
                    n = await fn(http, sid, now)
                    et, lm = self._validators.get(sid, (None, None))
                    self.store.record_source(sid, now=now, ok=True, items=n, etag=et,
                                             last_modified=lm)
                    results[sid] = f"ok ({n})"
                except Exception as exc:  # noqa: BLE001 — one bad source never stops the rest
                    msg = f"{type(exc).__name__}: {exc}"[:300]
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    self.store.record_source(sid, now=now, ok=False, error=msg,
                                             http_status=status)
                    results[sid] = "error: " + msg
                    log.warning("terror source %s failed: %s", sid, msg)
        return results

    async def _src_level(self, http, sid: str, now: datetime) -> int:
        spec = BY_ID[sid]
        _code, body = await self._get(http, sid, spec.urls[0])
        if body is None:
            return (self.store.source_state(sid) or {}).get("items") or 0
        self._cache(sid, body, "html")
        parser = {"mi5_threat_level": official.parse_mi5, "vigipirate": official.parse_vigipirate,
                  "nctv_threat_level": official.parse_nctv,
                  "asio_threat_level": official.parse_asio}[sid]
        levels = parser(body)
        for lv in levels:
            self.store.save_level({**lv, "source_id": sid}, now)
        return len(levels)

    async def _src_ntas(self, http, sid: str, now: datetime) -> int:
        try:
            _, feed = await self._get(http, sid, official.NTAS_FEED, conditional=False)
        except httpx.HTTPStatusError as exc:
            # dhs.gov (Akamai) refuses many non-browser TLS clients: fall back to the page via
            # AlterLab, the platform's route for bot-blocked official pages, when configured
            if not (self.alterlab_key and self.fetch_cfg is not None):
                raise httpx.HTTPStatusError(
                    f"{exc} (dhs.gov blocks this client; set ALTERLAB_API_KEY for the "
                    "AlterLab fallback)", request=exc.request, response=exc.response) from None
            from osint.pipeline.extract.fetch import fetch_alterlab

            txt, _ = await fetch_alterlab(http, official.NTAS_PAGE, self.alterlab_key,
                                          self.fetch_cfg)
            levels = official.parse_ntas_text(txt)
            for lv in levels:
                self.store.save_level({**lv, "source_id": sid, "via": "AlterLab (page text)"},
                                      now)
            return len(levels[0].get("advisories") or [])
        try:  # the page is only a cross-check of the feed
            _, page = await self._get(http, sid, official.NTAS_PAGE, conditional=False)
        except httpx.HTTPError:
            page = None
        levels = official.parse_ntas(feed or b"<alerts/>", page)
        for lv in levels:
            self.store.save_level({**lv, "source_id": sid}, now)
        return len(levels[0].get("advisories") or [])

    async def _src_advisories(self, http, sid: str, now: datetime) -> int:
        _, body = await self._get(http, sid, official.STATE_TA_URL)
        if body is None:
            return (self.store.source_state(sid) or {}).get("items") or 0
        items = official.parse_state_advisories(body)
        self.store.save_advisories(items, now)
        return len(items)

    def _link_designation(self, e: dict) -> dict:
        names = [*name_variants(e["name"]), *e.get("aliases", [])]
        gid, matched = self.kb.match_designation(names)
        if gid:
            e["group_id"], e["group_basis"] = gid, f"exact name match: '{matched}'"
        elif e.get("linked_to"):
            for lt in e["linked_to"]:
                gid, matched = self.kb.match_designation(name_variants(lt))
                if gid:
                    e["group_id"] = gid
                    e["group_basis"] = f"OFAC remark 'Linked To: {lt}'"
                    break
        if not e.get("group_id") and e["list_id"] in AUTO_GROUP_LISTS and \
                e.get("kind") == "organisation" and e.get("status") == "listed":
            g = self.kb.auto_group(e["name"], e.get("aliases") or [])
            e["group_id"], e["group_basis"] = g.id, "group created from this list entry"
        return e

    async def _src_designations(self, http, sid: str, now: datetime) -> int:
        spec = BY_ID[sid]
        _, body = await self._get(http, sid, spec.urls[0], conditional=False)
        h = hashlib.sha256(body).hexdigest()
        prev = self.store.source_state(sid) or {}
        parser = {"us_fto": des.parse_fto, "ofac_sdgt": des.parse_sdn, "un_1267_1988": des.parse_un,
                  "uk_proscribed": des.parse_uk, "eu_terrorist_list": des.parse_eu}[sid]
        entries = await asyncio.to_thread(parser, body)
        if not entries:
            raise ValueError("list parsed to 0 entries (format change?)")
        list_ids = sorted({e["list_id"] for e in entries}) or []
        if sid == "un_1267_1988":
            list_ids = ["un_1267", "un_1988"]
        if sid == "eu_terrorist_list":
            list_ids = ["eu_terr", "eu_isil_aq"]
        entries = [self._link_designation(e) for e in entries]
        first = not self.store.designations(list_id=list_ids[0], include_removed=True)
        if prev.get("content_hash") != h or first:
            self.store.upsert_designations(list_ids, entries, now, first_load=first)
            self._validators[sid] = (None, None)
            self.store.set_meta("auto_groups", self.kb.auto_groups(), now)
        self.store.record_source(sid, now=now, ok=True, items=len(entries), content_hash=h)
        if sid in ("us_fto", "uk_proscribed"):
            self._cache(sid, body, "html" if sid == "us_fto" else "json")
        return len(entries)

    async def _src_doj(self, http, sid: str, now: datetime) -> int:
        have = {d["id"] for d in self.store.docs(source="doj", limit=5000)}
        pages = self.cfg.sources.doj_backfill_pages if not have else \
            self.cfg.sources.doj_pages_per_pass
        new = 0
        for page in range(pages):
            _, body = await self._get(http, sid, feeds.DOJ_URL.format(page=page),
                                      conditional=False)
            docs = feeds.parse_doj(body or b"{}")
            docs = self._annotate_docs(_dedupe_doj(docs), country="US")
            new += self.store.save_docs(docs, now)
            import json as _json
            ids_on_page = [str(r.get("uuid")) for r in (_json.loads(body or b"{}").get("results")
                                                        or [])]
            if have and any(("doj:" + i) in have for i in ids_on_page):
                break
            if not ids_on_page:
                break
        return new

    def _annotate_docs(self, docs: list[dict], country: str | None = None) -> list[dict]:
        for d in docs:
            hits = self.kb.find(f"{d.get('title') or ''} {d.get('summary') or ''}")
            d["group_ids"] = [g for g, _ in hits]
            d["group_basis"] = "group name mentioned in the release text (mention, not " \
                               "attribution)" if hits else None
            if country:
                d["countries"] = [country]
                d["country_basis"] = "issuing jurisdiction"
        return docs

    async def _src_docs(self, http, sid: str, now: datetime) -> int:
        spec = BY_ID[sid]
        _, body = await self._get(http, sid, spec.urls[0])
        if body is None:
            return 0
        if sid == "europol_news":
            docs = feeds.parse_europol(body, now)
        else:
            docs = feeds.parse_research(body, sid)
        return self.store.save_docs(self._annotate_docs(docs), now)

    async def _src_ucdp(self, http, sid: str, now: datetime) -> int:
        from osint.backfill.ucdp import candidate_urls, discover

        _, page = await self._get(http, sid, BY_ID[sid].urls[0], conditional=False)
        html = (page or b"").decode("utf-8", errors="replace")
        urls = discover(html)
        ged = [u for u in urls if u.lower().endswith(".zip")]
        cands = candidate_urls(html)
        if not ged:
            raise ValueError("no GED download link found")
        sources = []
        body = await self._download_cached(http, ged[0])
        sources.append((Path(ged[0]).stem, ucdp.rows_from_zip(body)))
        # candidate releases oldest -> newest so later releases replace earlier rows
        for u in sorted(cands, key=_candidate_order):
            sources.append((Path(u).stem, ucdp.rows_from_csv(await self._download_cached(http, u))))
        rows = await asyncio.to_thread(ucdp.aggregate, sources, self.kb)
        self.store.replace_ucdp(rows)
        self.store.set_meta("ucdp_files", {"ged": ged[0], "candidates": cands}, now)
        return len(rows)

    async def _download_cached(self, http, url: str) -> bytes:
        name = Path(url).name
        if self.cache_dir is not None and (self.cache_dir / name).is_file():
            return (self.cache_dir / name).read_bytes()
        r = await http.get(url, headers={"User-Agent": BOT_UA})
        r.raise_for_status()
        if self.cache_dir is not None:
            self._cache_named(name, r.content)
        return r.content

    def _cache_named(self, name: str, body: bytes) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / (name + ".part")).write_bytes(body)
            (self.cache_dir / (name + ".part")).replace(self.cache_dir / name)
        except OSError:
            log.debug("cache write failed for %s", name, exc_info=True)

    # =============================================================== events

    def load_window(self, now: datetime) -> tuple[list[dict], dict[str, list[dict]]]:
        since = now - timedelta(days=self.cfg.window_days)
        evs = self.events.recent_events(since)
        docs = self.events.event_documents_for([e["id"] for e in evs]) if evs else {}
        return evs, docs

    def candidates(self, now: datetime) -> list[dict]:
        """Events worth (or already given) a terror classification, with their titles."""
        evs, docs = self.load_window(now)
        out = []
        for e in evs:
            titles = " \n ".join([e.get("title") or "", *[d.get("title") or ""
                                                         for d in docs.get(e["id"], [])]])
            if text.is_candidate(titles):
                e["_titles"] = titles
                out.append(e)
        return out

    def select_llm(self, cands: list[dict], now: datetime) -> list[extract.ExtractItem]:
        from osint.pipeline.extract.job import best_document

        if not self.cfg.llm.enabled or self.backend is None:
            return []
        done = self.store.extractions([e["id"] for e in cands])
        base = self.base_store.get_many([e["id"] for e in cands]) if self.base_store else {}
        due = []
        retry = timedelta(minutes=self.cfg.llm.retry_after_min)
        for e in cands:
            if text.is_denial(e.get("title")) or text.state_warfare_only(e.get("title")):
                continue  # excluded by rule: no call spent
            b = base.get(e["id"]) or {}
            if b.get("status") == "done" and (b.get("result") or {}).get("is_real_event") is False:
                continue
            rec = done.get(e["id"])
            if rec is not None:
                if rec["status"] == "done":
                    continue
                upd = datetime.fromisoformat(rec["updated_at"])
                if rec["attempts"] >= self.cfg.llm.max_attempts or upd + retry > now:
                    continue
            due.append(e)
        # newest first (event time), then: terrorism named in a headline, more outlets
        due.sort(key=lambda e: e.get("t_end") or "", reverse=True)
        due.sort(key=lambda e: (not text.STRONG_TERROR.search(e.get("_titles") or ""),
                                -(e.get("source_diversity") or 0)))
        items = []
        for e in due[: self.cfg.llm.max_per_run]:
            full = self.events.get_event(e["id"])
            doc = best_document((full or {}).get("documents") or [])
            if doc is not None:
                items.append(extract.ExtractItem(full, doc))
        return items

    async def extract_pass(self, items: list[extract.ExtractItem], now: datetime) -> dict:
        from osint.llm import LLMError

        stats = {"calls": 0, "ok": 0, "errors": 0, "cost_usd": 0.0, "capped": False}
        if not items:
            return stats
        async with self.http_factory() as http:
            sem = asyncio.Semaphore(max(1, self.cfg.llm.concurrency))

            async def one(it: extract.ExtractItem) -> None:
                async with sem:
                    if not self.limiter.try_acquire():
                        stats["capped"] = True
                        return
                    stats["calls"] += 1
                    eid = it.event["id"]
                    prev = self.store.extraction(eid)
                    attempts = (prev or {}).get("attempts", 0) + 1
                    try:
                        res, meta = await extract.extract_one(
                            self.backend, http, it, fetch_cfg=self.fetch_cfg,
                            alterlab_key=self.alterlab_key)
                    except (LLMError, NotImplementedError, OSError, httpx.HTTPError) as exc:
                        err = f"{type(exc).__name__}: {exc}"
                        self.store.log_call(self.clock(), event_id=eid, ok=False, error=err)
                        self.store.save_extraction(eid, now=utcnow(), status="error",
                                                   doc_id=it.doc.get("doc_id"),
                                                   url=it.doc.get("url"), error=err,
                                                   attempts=attempts)
                        stats["errors"] += 1
                        return
                    res["_model"] = meta["model"]
                    self.store.log_call(self.clock(), event_id=eid, ok=True,
                                        cost_usd=meta["cost_usd"], latency_s=meta["latency_s"],
                                        model=meta["model"])
                    self.store.save_extraction(eid, now=utcnow(), status="done",
                                               doc_id=meta["doc_id"], url=meta["url"], result=res,
                                               cost_usd=meta["cost_usd"],
                                               latency_s=meta["latency_s"], model=meta["model"],
                                               attempts=attempts)
                    stats["ok"] += 1
                    stats["cost_usd"] += meta["cost_usd"] or 0.0

            await asyncio.gather(*(one(i) for i in items))
        stats["cost_usd"] = round(stats["cost_usd"], 6)
        return stats

    # =============================================================== incidents

    def derive(self, now: datetime | None = None) -> dict:
        now = now or utcnow()
        evs, docs = self.load_window(now)
        by_id = {e["id"]: e for e in evs}
        cands = []
        for e in evs:
            titles = " \n ".join([e.get("title") or "", *[d.get("title") or ""
                                                         for d in docs.get(e["id"], [])]])
            if text.is_candidate(titles):
                cands.append(e["id"])
        terr = self.store.extractions(cands)
        base = self.base_store.get_many(cands) if self.base_store else {}
        records: list[dict] = []
        full: dict[str, dict] = {}
        for eid in cands:
            ev = self.events.get_event(eid, follow=False)
            if ev is None:
                continue
            full[eid] = ev
            t = (terr.get(eid) or {})
            tres = t.get("result") if t.get("status") == "done" else None
            rec = cls.classify(ev, base.get(eid), tres, kb=self.kb, geocode=self.geocoder,
                               now=now)
            if rec is not None:
                records.append(rec)
        # incident-level duplicates
        vecs = {}
        try:
            from osint.pipeline.cluster import _unpack_vec
            for e in evs:
                v = (e.get("cluster_state") or {}).get("vector")
                if v:
                    vecs[e["id"]] = _unpack_vec(v)
        except Exception:  # vectors are optional
            log.debug("no event vectors", exc_info=True)
        pairs = dedupe.merge(records, vecs, max_hours=self.cfg.dedupe.max_hours,
                             max_km=self.cfg.dedupe.max_km, min_cosine=self.cfg.dedupe.min_cosine)
        by_rec = {r["id"]: r for r in records}
        merged_into: dict[str, list[tuple[str, str]]] = {}
        for gone, keep, why in pairs:
            by_rec[gone]["merged_into"] = keep
            by_rec[gone]["merge_basis"] = why
            merged_into.setdefault(keep, []).append((gone, why))
        for keep, gones in merged_into.items():
            r = by_rec[keep]
            ev = dict(full[r["event_id"]])
            ev["documents"] = list(ev.get("documents") or [])
            for gone, _ in gones:
                ev["documents"] += full[by_rec[gone]["event_id"]].get("documents") or []
            t = (terr.get(r["event_id"]) or {})
            again = cls.classify(ev, base.get(r["event_id"]),
                                 t.get("result") if t.get("status") == "done" else None,
                                 kb=self.kb, geocode=self.geocoder, now=now)
            if again is not None:
                again["merged_from"] = [{"id": g, "basis": w} for g, w in gones]
                by_rec[keep] = again
        records = list(by_rec.values())
        # drop incidents of in-window events that are no longer candidates / were merged
        # away by the clusterer; incidents of events outside the window stay (history)
        keep_ids = {r["id"] for r in records}
        stale = []
        window_ids = {"ti-" + e for e in by_id}
        for iid in self.store.incident_ids():
            if iid in window_ids and iid not in keep_ids:
                stale.append(iid)
            elif iid not in keep_ids and iid not in window_ids:
                eid = iid[3:]
                if self.events.resolve_event_id(eid) not in (None, eid):
                    stale.append(iid)
        self.store.delete_incidents(stale)
        self.store.replace_incidents(records, now)
        first = min((e.get("first_seen") or e.get("t_start") or "" for e in evs), default="")
        cov = self.store.get_meta("news_coverage_start")
        if first and (cov is None or first < cov):
            self.store.set_meta("news_coverage_start", first, now)
        counts: dict[str, int] = {}
        for r in records:
            if r.get("merged_into"):
                counts["merged"] = counts.get("merged", 0) + 1
                continue
            k = f"{r.get('klass') or 'none'}:{r['status']}"
            counts[k] = counts.get(k, 0) + 1
        return {"events_in_window": len(evs), "candidates": len(cands),
                "incidents": len(records), "merged_pairs": len(pairs), "deleted": len(stale),
                "counts": counts}

    # =============================================================== indicators

    def coverage_days(self, now: datetime) -> float:
        cov = self.store.get_meta("news_coverage_start")
        if not cov:
            return 0.0
        try:
            dt = datetime.fromisoformat(cov)
        except ValueError:
            return 0.0
        dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        return max(0.0, (now - dt).total_seconds() / 86400)

    def compute_indicators(self, now: datetime | None = None) -> dict:
        from osint.terror.board import build_scopes

        now = now or utcnow()
        return build_scopes(self.store, self.kb, now=now, cfg=self.cfg.indicators,
                            coverage_days=self.coverage_days(now), calendar=self.calendar)

    # =============================================================== loop

    async def run_once(self, now: datetime | None = None) -> dict:
        now = now or utcnow()
        t0 = time.monotonic()
        src = await self.refresh_sources(now)
        cands = await asyncio.to_thread(self.candidates, now)
        items = await asyncio.to_thread(self.select_llm, cands, now)
        llm = await self.extract_pass(items, now)
        derived = await asyncio.to_thread(self.derive, now)
        ind = await asyncio.to_thread(self.compute_indicators, now)
        self.passes += 1
        self.last_pass = {"at": now.isoformat(timespec="seconds"), "sources": src, "llm": llm,
                          "derive": derived, "indicators": ind,
                          "seconds": round(time.monotonic() - t0, 1)}
        self.store.set_meta(STATUS_KEY, self.status(), now)
        return self.last_pass

    async def run(self) -> None:
        if not self.cfg.enabled:
            log.info("terror watch disabled (config/terror.yaml)")
            return
        await asyncio.sleep(self.cfg.initial_delay_s)
        while True:
            try:
                await self.run_once()
                self.last_error = None
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("terror watch pass failed")
                try:
                    self.store.set_meta(STATUS_KEY, self.status())
                except Exception:
                    log.exception("terror status write failed")
            await asyncio.sleep(self.cfg.interval_s)

    def status(self) -> dict:
        now = self.clock()
        day = self.store.cost_since(now - 86400)
        total = self.store.cost_since(0)
        return {"enabled": self.cfg.enabled, "passes": self.passes,
                "last_pass": self.last_pass, "last_error": self.last_error,
                "llm": {"backend": getattr(self.backend, "name", None),
                        "last_24h": day, "total": total,
                        "max_per_hour": self.cfg.llm.max_calls_per_hour,
                        "max_per_day": self.cfg.llm.max_calls_per_day},
                "counts": self.store.status_counts()}


def _dedupe_doj(docs: list[dict]) -> list[dict]:
    """USAO and NSD often publish the same release: keep one per (title, date)."""
    seen: dict[tuple, dict] = {}
    for d in docs:
        key = (d["title"].casefold(), (d.get("published_at") or "")[:10])
        if key in seen:
            seen[key].setdefault("also_urls", []).append(d.get("url"))
            continue
        seen[key] = d
    return list(seen.values())


def _candidate_order(url: str) -> tuple:
    """GEDEvent_v26_01_26_09 (cumulative Jan-Sep) before GEDEvent_v26_0_10 (October)."""
    import re

    nums = [int(x) for x in re.findall(r"\d+", Path(url).stem)]
    return (nums[0] if nums else 0, nums[-1] if nums else 0, len(nums))


# ------------------------------------------------------------------- wiring


def build_job(runtime) -> TerrorJob:
    from osint.enrich.gazetteer import CountryCities
    from osint.llm import make_backend
    from osint.pipeline.extract.config import ExtractionConfig
    from osint.pipeline.extract.job import get_store as get_extraction_store
    from osint.pipeline.extract.verdict import gazetteer_geocoder

    settings = runtime.settings
    cfg = TerrorConfig.load(settings.config_dir / "terror.yaml")
    xcfg = ExtractionConfig.load(settings.config_dir / "extraction.yaml")
    kw = {"model": xcfg.model}
    if xcfg.backend == "claude_cli":
        kw.update(timeout_s=xcfg.timeout_s, concurrency=cfg.llm.concurrency,
                  cli_path=xcfg.cli_path, cwd=settings.data_dir / "extraction_cwd")
    backend = make_backend(xcfg.backend, **kw) if cfg.llm.enabled else None
    gz = getattr(getattr(runtime, "triage", None), "gazetteer", None)
    geocode = gazetteer_geocoder(gz, CountryCities(settings.data_dir / "gazetteer")) \
        if gz is not None else None
    store = get_store(runtime)
    kb = GroupKB.load(settings.config_dir / "terror_groups.yaml")
    kb.add_auto(store.get_meta("auto_groups") or [])
    return TerrorJob(runtime.store, store, cfg, kb,
                     base_store=get_extraction_store(runtime), backend=backend,
                     geocoder=geocode, fetch_cfg=xcfg.fetch,
                     alterlab_key=settings.alterlab_api_key,
                     calendar=load_calendar(settings.config_dir / "terror_calendar.yaml"),
                     cache_dir=settings.data_dir / "terror_cache")


def start(runtime) -> asyncio.Task:
    job = build_job(runtime)
    runtime.terror_job = job
    task = asyncio.create_task(job.run(), name="terror")
    runtime._tasks.append(task)
    return task
