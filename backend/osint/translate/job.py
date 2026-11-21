"""TranslateJob: English versions of event titles/summaries and document titles/snippets.

Every ``interval_s``: scan events (the first pass after start covers all of them = backfill;
later passes only events updated since the previous pass), register each field's text in
the cache (``refs``) — English is stored as passthrough — then translate the queue in
batches: map-visible events first, event fields before document fields, recent first.
"""

import asyncio
import logging
import time
from collections import deque
from datetime import UTC, datetime, timedelta

from osint.llm import CallRateLimiter, LLMBackend, make_backend
from osint.pipeline.triage import detect_language
from osint.translate.batch import BatchTranslator
from osint.translate.config import TranslateConfig
from osint.translate.store import TranslationStore, clean, norm_lang, text_key

log = logging.getLogger(__name__)
ROLE_RANK = {"primary": 0, "mention": 1, "gdelt": 2}
SINCE_MARGIN = timedelta(minutes=10)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def is_english(text: str, lang: str) -> bool:
    """Tagged English, or untagged text detected as English. A non-English tag wins over
    detection (the stopword heuristic calls e.g. short German/Turkish headlines English)."""
    lang = norm_lang(lang)
    return lang == "en" or (not lang and detect_language(text) == "en")


def event_lang(ev: dict, docs: list[dict]) -> str:
    """Language of the event's title: its primary document's, else detected."""
    ranked = sorted(docs, key=lambda d: (ROLE_RANK.get(d.get("role"), 3),
                                         d.get("published_at") or ""))
    for d in ranked:
        if (d.get("title") or "") == (ev.get("title") or "") and d.get("language"):
            return norm_lang(d["language"])
    if ranked and ranked[0].get("language"):
        return norm_lang(ranked[0]["language"])
    return detect_language(f"{ev.get('title') or ''} {ev.get('summary') or ''}")


def lake_snippets(lake, docs: list[dict], span_h: float = 6.0) -> dict[str, str]:
    """{doc_id: snippet} from the documents lake, one query per window of documents."""
    dated = sorted((p, d["doc_id"]) for d in docs
                    if (p := _parse(d.get("published_at"))) is not None)
    out: dict[str, str] = {}
    i = 0
    while i < len(dated):
        j = i
        while j + 1 < len(dated) and j + 1 - i < 200 and \
                dated[j + 1][0] - dated[i][0] <= timedelta(hours=span_h):
            j += 1
        ids = [d for _, d in dated[i:j + 1]]
        rows = lake.query(
            f"SELECT id, snippet FROM documents WHERE id IN ({','.join('?' * len(ids))})",
            ids, start=dated[i][0] - timedelta(hours=1), end=dated[j][0] + timedelta(hours=1))
        out.update({r[0]: r[1] or "" for r in rows})
        i = j + 1
    return out


class TranslateJob:
    def __init__(self, event_store, store: TranslationStore, cfg: TranslateConfig,
                 backend: LLMBackend, *, limiter: CallRateLimiter | None = None,
                 shared_limiter: CallRateLimiter | None = None, snippets=None,
                 clock=time.time) -> None:
        self.event_store, self.store, self.cfg, self.backend = event_store, store, cfg, backend
        self.clock = clock
        self.limiter = limiter or CallRateLimiter(
            cfg.max_calls_per_hour, cfg.max_calls_per_day, clock=clock,
            history=store.call_times_since(clock() - 86400))
        self.shared_limiter = shared_limiter
        self.snippets = snippets  # docs -> {doc_id: snippet} (None: no document snippets)
        self.translator = BatchTranslator(backend, acquire=self._acquire)
        self._sem = asyncio.Semaphore(max(1, min(2, cfg.concurrency)))
        self._since: datetime | None = None
        self.passes = 0
        self.capped = False
        self.queue_size = 0
        self.last_run_at: str | None = None
        self.last_error: str | None = None
        self.last_scan: dict = {}
        self.errors: deque[dict] = deque(maxlen=10)

    # --------------------------------------------------------------- caps

    def _acquire(self) -> bool:
        sh = self.shared_limiter
        if sh is not None:
            c = sh.counts()
            if c["hour"] >= sh.per_hour or c["day"] >= sh.per_day:
                self.capped = True
                return False
        if not self.limiter.try_acquire():
            self.capped = True
            return False
        if sh is not None:
            sh.try_acquire()
        return True

    # --------------------------------------------------------------- scan

    def _events(self, since: datetime | None) -> list[dict]:
        cfg = self.cfg
        # curated history (UCDP, origin historical) is English already: never scanned
        events = self.event_store.list_events(since=since, limit=cfg.max_events, slim=False,
                                              origin="news,sensor")
        skip = set(cfg.skip_statuses or [])
        events = [e for e in events if e.get("status") not in skip]
        visible = {e["id"] for e in self.event_store.list_events(
            since=since, limit=cfg.max_events, include_unlocated=False, map_default=True,
            origin="news,sensor")}
        for e in events:
            e["_map"] = e["id"] in visible
        # stable sort keeps most-recently-updated first within each group
        return sorted(events, key=lambda e: not e["_map"])

    def scan(self, now: datetime | None = None, since: datetime | None = None,
             chunk: int = 200) -> dict:
        """Register every event/document field text (chunks of events, so memory stays
        flat); returns scan counts."""
        now = now or utcnow()
        events = self._events(since)
        totals = {"events": len(events), "texts": 0, "new": 0, "snippet_lookups": 0}
        for i in range(0, len(events), chunk):
            part = self._scan_chunk(events[i:i + chunk], now)
            for k, v in part.items():
                totals[k] += v
        return totals

    def _scan_chunk(self, events: list[dict], now: datetime) -> dict:
        mc = self.cfg.max_chars
        items: list[dict] = []
        refs: list[tuple[str, str, str]] = []

        def add(ref: str, text: str | None, lang: str, prio: int, sort_ts: str) -> None:
            t = clean(text, mc)
            if not t:
                return
            key = text_key(lang, t)
            refs.append((ref, key, lang))
            items.append({"key": key, "lang": lang, "text": t, "prio": prio,
                          "sort_ts": sort_ts, "english": is_english(t, lang)})

        full_events = [(e, (self.event_store.get_event(e["id"]) or {}).get("documents") or [])
                       for e in events]
        have = self.store.existing_refs(
            [f"d:{d['doc_id']}:snippet" for _, docs in full_events for d in docs])
        pending_docs: dict[str, dict] = {}
        for e, docs in full_events:
            tier = 0 if e["_map"] else 2
            ts = e.get("updated_at") or ""
            lang = event_lang(e, docs)
            add(f"e:{e['id']}:title", e.get("title"), lang, tier, ts)
            add(f"e:{e['id']}:summary", e.get("summary"), lang, tier, ts)
            for d in docs:
                dl = norm_lang(d.get("language")) or detect_language(d.get("title") or "")
                add(f"d:{d['doc_id']}:title", d.get("title"), dl, tier + 1, ts)
                if f"d:{d['doc_id']}:snippet" not in have and d["doc_id"] not in pending_docs:
                    pending_docs[d["doc_id"]] = {**d, "_lang": dl, "_tier": tier + 1,
                                                 "_ts": ts}
        if self.snippets is not None and pending_docs:
            try:
                snips = self.snippets(list(pending_docs.values()))
            except Exception:
                log.exception("document snippet lookup failed")
                snips = {}
            for doc_id, d in pending_docs.items():
                if doc_id in snips:
                    add(f"d:{doc_id}:snippet", snips[doc_id], d["_lang"], d["_tier"], d["_ts"])
        added = self.store.enqueue(items, now)
        self.store.set_refs(refs, now)
        return {"texts": len(items), "new": added, "snippet_lookups": len(pending_docs)}

    # --------------------------------------------------------------- translate

    async def translate_queue(self, now: datetime | None = None,
                              max_calls: int | None = None) -> dict:
        """Translate up to ``max_calls`` batches from the queue."""
        cfg = self.cfg
        max_calls = cfg.max_calls_per_pass if max_calls is None else max_calls
        now = now or utcnow()
        due = await asyncio.to_thread(
            self.store.due, cfg.batch_size * max_calls, now,
            timedelta(minutes=cfg.retry_after_min), cfg.max_attempts)
        batches = [due[i:i + cfg.batch_size] for i in range(0, len(due), cfg.batch_size)]
        totals = {"calls": 0, "done": 0, "failed": 0, "cost_usd": 0.0}

        async def one(batch: list[dict]) -> None:
            async with self._sem:
                if self.capped:
                    return
                out = await self.translator.translate(batch)
            t = utcnow()
            for c in out.calls:
                self.store.log_call(self.clock(), ok=c["ok"], n_items=c["n"], n_ok=c["n_ok"],
                                    cost_usd=c.get("cost_usd", 0.0),
                                    latency_s=c.get("latency_s"), model=c.get("model"),
                                    error=c.get("error"))
                totals["calls"] += 1
                totals["cost_usd"] += c.get("cost_usd", 0.0)
                if c.get("error"):
                    self.errors.appendleft({"at": t.isoformat(timespec="seconds"),
                                            "items": c["n"], "error": c["error"][:300]})
            if out.done:
                self.store.save_done(out.done, out.model or getattr(self.backend, "model", ""),
                                     t)
            by_err: dict[str, list[str]] = {}
            for k, err in out.failed.items():
                by_err.setdefault(err, []).append(k)
            for err, keys in by_err.items():
                self.store.save_failed(keys, err, t)
            totals["done"] += len(out.done)
            totals["failed"] += len(out.failed)

        await asyncio.gather(*(one(b) for b in batches))
        return totals

    async def run_once(self, max_calls: int | None = None) -> dict:
        self.capped = False
        start = utcnow()
        scan = await asyncio.to_thread(self.scan, start, self._since)
        self.last_scan = scan
        res = await self.translate_queue(start, max_calls)
        self._since = start - SINCE_MARGIN
        self.queue_size = await asyncio.to_thread(
            self.store.queue_size, utcnow(), timedelta(minutes=self.cfg.retry_after_min),
            self.cfg.max_attempts)
        self.last_run_at = utcnow().isoformat(timespec="seconds")
        return {**scan, **res, "queue": self.queue_size, "capped": self.capped}

    async def run(self) -> None:
        if not self.cfg.enabled:
            log.info("translation disabled (config/translate.yaml)")
            return
        await asyncio.sleep(self.cfg.initial_delay_s)
        while True:
            try:
                await self.run_once()
                self.last_error = None
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("translation pass failed")
            self.passes += 1
            await asyncio.sleep(self.cfg.interval_s)

    # --------------------------------------------------------------- status

    def status(self) -> dict:
        return status(self.store, self.cfg, self.clock, job=self)


def status(store: TranslationStore, cfg: TranslateConfig, clock=time.time, job=None) -> dict:
    now = clock()
    day_start = datetime.fromtimestamp(now, UTC).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    counts = store.status_counts()
    out = {
        "enabled": cfg.enabled, "model": cfg.model, "running": job is not None,
        "calls": {"today_utc": store.call_stats(day_start), "last_24h": store.call_stats(
            now - 86400), "total": store.call_stats(0),
            "max_per_hour": cfg.max_calls_per_hour, "max_per_day": cfg.max_calls_per_day},
        "texts": counts,
        "queue": store.queue_size(datetime.fromtimestamp(now, UTC),
                                  timedelta(minutes=cfg.retry_after_min), cfg.max_attempts),
        "failures": {"count": counts.get("failed", 0), "recent": store.last_failures()},
    }
    if job is not None:
        out["calls"]["hour"] = job.limiter.counts()["hour"]
        out.update(passes=job.passes, capped=job.capped, last_run_at=job.last_run_at,
                   last_error=job.last_error, last_scan=job.last_scan,
                   last_errors=list(job.errors)[:5])
    return out


# ------------------------------------------------------------------- wiring


def get_store(runtime) -> TranslationStore | None:
    """The runtime's translation cache, created on first use (shared by job and API)."""
    store = getattr(runtime, "translation_store", None)
    if store is None:
        settings = getattr(runtime, "settings", None)
        if settings is None:
            return None
        store = TranslationStore(settings.data_dir / "translations.sqlite")
        runtime.translation_store = store
    return store


def load_config(runtime) -> TranslateConfig:
    settings = getattr(runtime, "settings", None)
    if settings is None:
        return TranslateConfig()
    return TranslateConfig.load(settings.config_dir / "translate.yaml")


def build_job(runtime) -> TranslateJob:
    settings = runtime.settings
    cfg = load_config(runtime)
    kwargs = {"model": cfg.model}
    if cfg.backend == "claude_cli":
        kwargs.update(timeout_s=cfg.timeout_s, concurrency=cfg.concurrency,
                      cli_path=cfg.cli_path, cwd=settings.data_dir / "translate_cwd")
    backend = make_backend(cfg.backend, **kwargs)
    shared = None
    if cfg.share_extraction_limiter:
        shared = getattr(getattr(runtime, "extraction_job", None), "limiter", None)
    lake = getattr(runtime, "lake", None)
    return TranslateJob(runtime.store, get_store(runtime), cfg, backend,
                        shared_limiter=shared,
                        snippets=None if lake is None else (lambda docs: lake_snippets(lake,
                                                                                      docs)))


def start(runtime) -> asyncio.Task:
    """Create runtime.translate_job and start its loop (one-line wiring from Runtime._boot)."""
    job = build_job(runtime)
    runtime.translate_job = job
    task = asyncio.create_task(job.run(), name="translate")
    runtime._tasks.append(task)
    return task
